import os
import json
from typing import Optional, Dict

import pandas as pd

from mindsdb.integrations.handlers.openai_handler.openai_handler import OpenAIHandler
from mindsdb.integrations.libs.llm.utils import ft_jsonl_validation, ft_formatter
from mindsdb.integrations.utilities.handler_utils import get_api_key
from mindsdb.utilities import log

logger = log.getLogger(__name__)


ANYSCALE_API_BASE = 'https://api.endpoints.anyscale.com/v1'
MIN_FT_VAL_LEN = 20  # anyscale checks for at least 20 validation chats
MIN_FT_DATASET_LEN = MIN_FT_VAL_LEN * 2  # we ask for 20 training chats as well


class AnyscaleEndpointsHandler(OpenAIHandler):
    name = 'anyscale_endpoints'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.all_models = []
        self.chat_completion_models = []
        self.supported_ft_models = []
        self.default_model = 'meta-llama/Llama-2-7b-chat-hf'
        self.api_base = ANYSCALE_API_BASE
        self.default_mode = 'default'  # can also be 'conversational' or 'conversational-full'
        self.supported_modes = ['default', 'conversational', 'conversational-full']
        self.rate_limit = 25  # requests per minute
        self.max_batch_size = 20
        self.default_max_tokens = 100

    @staticmethod
    def create_validation(target, args=None, **kwargs):
        if 'using' not in args:
            raise Exception(
                "Anyscale Endpoints engine requires a USING clause! Refer to its documentation for more details."
            )
        else:
            args = args['using']

        engine_storage = kwargs['handler_storage']
        connection_args = engine_storage.get_connection_args()
        api_key = get_api_key('anyscale_endpoints', args, engine_storage=engine_storage)
        api_base = connection_args.get('api_base') or args.get('api_base') or os.environ.get('ANYSCALE_API_BASE', ANYSCALE_API_BASE)

        client = OpenAIHandler._get_client(api_key=api_key, base_url=api_base)
        OpenAIHandler._check_client_connection(client)

    def create(self, target, args=None, **kwargs):
        # load base and fine-tuned models, then hand over
        self._set_models_from_args(args)
        super().create(target, args, **kwargs)

    def predict(self, df: pd.DataFrame, args: Optional[Dict] = None) -> pd.DataFrame:
        # load base and fine-tuned models, then hand over
        self._set_models_from_args(args)
        return super().predict(df, args)

    def finetune(self, df: Optional[pd.DataFrame] = None, args: Optional[Dict] = None) -> None:
        using_args = args.get('using', {})
        self._set_models(using_args)
        super().finetune(df, args)

        # rewrite chat_completion_models to include the newly fine-tuned model
        args = self.model_storage.json_get('args')
        args['chat_completion_models'] = list(self.chat_completion_models) + [args['model_name']]
        self.model_storage.json_set('args', args)

    def describe(self, attribute: Optional[str] = None) -> pd.DataFrame:
        args = self.model_storage.json_get('args')

        # keys are not shown
        for arg in ('api_key', 'openai_api_key'):
            if arg in args:
                del args[arg]

        if attribute == 'args':
            return pd.DataFrame(args.items(), columns=['key', 'value'])
        elif attribute == 'metadata':
            # we opt for the URL because some models require completing a form to access their artifacts
            model_name = args.get('model_name', self.default_model)
            model_card_url = 'https://huggingface.co/' + model_name
            return pd.DataFrame({'model_name': [model_name], 'model_card': [model_card_url]})
        else:
            tables = ['args', 'metadata']
            return pd.DataFrame(tables, columns=['tables'])

    def _set_models(self, args):
        api_key = get_api_key('anyscale_endpoints', args, self.engine_storage)
        client = OpenAIHandler._get_client(api_key=api_key, base_url=self.api_base)
        self.all_models = [m.id for m in client.models.list()]
        self.chat_completion_models = [m.id for m in client.models.list() if m.rayllm_metadata['engine_config']['model_type'] == 'text-generation']  # noqa
        self.supported_ft_models = self.chat_completion_models  # base models compatible with fine-tuning

    def _set_models_from_args(self, args):
        self._set_models(args.get('using', {}))

        model_args = self.model_storage.json_get('args')
        if model_args and 'chat_completion_models' in model_args:
            self.chat_completion_models = model_args.get('chat_completion_models')

    @staticmethod
    def _prepare_ft_jsonl(df, temp_storage_path, temp_filename, _, test_size=0.2):
        # 1. format data
        chats = ft_formatter(df)

        # 2. split chats in training and validation subsets
        series = pd.Series(chats)
        if len(series) < MIN_FT_DATASET_LEN:
            raise Exception(f"Dataset is too small to finetune. Please include at least {MIN_FT_DATASET_LEN} samples (complete chats).")
        val_size = max(MIN_FT_VAL_LEN, int(len(series) * test_size))  # at least as many samples as required by Anyscale
        train = series.iloc[:-val_size]
        val = series.iloc[-val_size:]

        # 3. write as jsonl files
        file_names = {
            'train': f'{temp_filename}_prepared_train.jsonl',
            'val': f'{temp_filename}_prepared_valid.jsonl',
        }
        train.to_json(os.path.join(temp_storage_path, file_names['train']), orient='records', lines=True)
        val.to_json(os.path.join(temp_storage_path, file_names['val']), orient='records', lines=True)

        # 5. validate and return
        with open(os.path.join(temp_storage_path, file_names['train']), 'r', encoding='utf-8') as f:
            ft_jsonl_validation([json.loads(line) for line in f])

        with open(os.path.join(temp_storage_path, file_names['val']), 'r', encoding='utf-8') as f:
            ft_jsonl_validation([json.loads(line) for line in f])

        return file_names

    def _get_ft_model_type(self, model_name: str):
        for base_model in self.chat_completion_models:
            if base_model.lower() in model_name.lower():
                return base_model
        logger.warning(f'Cannot recognize model {model_name}. Finetuning may fail.')
        return model_name.lower()

    @staticmethod
    def _add_extra_ft_params(ft_params, using_args):
        hyperparameters = {}
        # we populate separately because keys with `None` break the API
        for key in ('n_epochs', 'context_length'):
            if using_args.get(key, None):
                hyperparameters[key] = using_args[key]
        if hyperparameters:
            return {**ft_params, **{'hyperparameters': hyperparameters}}
        else:
            return ft_params
