from __future__ import annotations

import traceback

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer


class Embedding:
    _instance = None
    _model = None
    _tokenizer = None
    _device = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        if not cls._initialized:
            try:
                print("Initializing embedding model...")
                cls._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                model_name = "jinaai/jina-embeddings-v2-base-code"
                cls._tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
                cls._model = AutoModel.from_pretrained(model_name, trust_remote_code=True).to(cls._device)
                cls._model.eval()
                cls._initialized = True
                print(f"Embedding model initialized on {cls._device}")
            except Exception as e:
                print(f"Embedding model initialization failed: {e}")
                raise
        return cls._instance

    def __init__(self):
        return

    def get_embedding(self, text):
        try:
            if text is None:
                print("Warning: embedding input is None")
                return None
            if not isinstance(text, str):
                print(f"Warning: embedding input is not str, got {type(text)}")
                text = str(text)
            if not text.strip():
                print("Warning: embedding input is empty")
                return None
            if self._model is None or self._tokenizer is None:
                raise RuntimeError("Embedding model is not initialized")

            inputs = self._tokenizer(
                text,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            inputs = {key: value.to(self._device) for key, value in inputs.items()}
            with torch.no_grad():
                outputs = self._model(**inputs)
                hidden = outputs.last_hidden_state
                mask = inputs["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
                summed = torch.sum(hidden * mask, dim=1)
                counts = torch.clamp(mask.sum(dim=1), min=1e-9)
                embedding = summed / counts
                embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
            return embedding[0].detach().cpu().tolist()
        except Exception as e:
            print(f"Error while getting embedding: {e}")
            print(f"model state: {self._model}")
            print(traceback.format_exc())
            return None

    def _cos_similarity(self, vec1, vec2):
        return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

    def text_similarity(self, text1, text2):
        vec1 = self.get_embedding(text1)
        vec2 = self.get_embedding(text2)
        return self._cos_similarity(vec1, vec2)
