from typing import Optional, Dict, Tuple
from backend_config import backend_model, backend_top_k

import numpy as np
import os
import torch
from transformers import AutoModel, AutoTokenizer

from jina import Executor, DocumentArray, requests, Document

## READ THIS: This is almost copied 100% from the chatbot example. I don't understand what's _actually_ happening in these Executors, especially the Transformer.
# When Jina Hub 2.0 is released we can remove this file and simply get Executors direct from the Hub
# The Executors below are deliberately simple and not built for power use (e.g. indexer stores everything in memory, so have to re-index on every run)


class MyTransformer(Executor):
    """Transformer executor class """

    def __init__(
        self,
        pretrained_model_name_or_path: str = backend_model,
        base_tokenizer_model: Optional[str] = None,
        pooling_strategy: str = "mean",
        layer_index: int = -1,
        max_length: Optional[int] = None,
        acceleration: Optional[str] = None,
        embedding_fn_name: str = "__call__",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.pretrained_model_name_or_path = pretrained_model_name_or_path
        self.base_tokenizer_model = (
            base_tokenizer_model or pretrained_model_name_or_path
        )
        self.pooling_strategy = pooling_strategy
        self.layer_index = layer_index
        self.max_length = max_length
        self.acceleration = acceleration
        self.embedding_fn_name = embedding_fn_name
        self.tokenizer = AutoTokenizer.from_pretrained(self.base_tokenizer_model)
        self.model = AutoModel.from_pretrained(
            self.pretrained_model_name_or_path, output_hidden_states=True
        )
        self.model.to(torch.device("cpu"))

    def _compute_embedding(self, hidden_states: "torch.Tensor", input_tokens: Dict):
        import torch

        fill_vals = {"cls": 0.0, "mean": 0.0, "max": -np.inf, "min": np.inf}
        fill_val = torch.tensor(
            fill_vals[self.pooling_strategy], device=torch.device("cpu")
        )

        layer = hidden_states[self.layer_index]
        attn_mask = input_tokens["attention_mask"].unsqueeze(-1).expand_as(layer)
        layer = torch.where(attn_mask.bool(), layer, fill_val)

        embeddings = layer.sum(dim=1) / attn_mask.sum(dim=1)
        return embeddings.cpu().numpy()

    @requests
    def encode(self, docs: "DocumentArray", *args, **kwargs):
        import torch

        with torch.no_grad():

            if not self.tokenizer.pad_token:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
                self.model.resize_token_embeddings(len(self.tokenizer.vocab))

            input_tokens = self.tokenizer(
                docs.get_attributes("content"),
                max_length=self.max_length,
                padding="longest",
                truncation=True,
                return_tensors="pt",
            )
            input_tokens = {
                k: v.to(torch.device("cpu")) for k, v in input_tokens.items()
            }

            outputs = getattr(self.model, self.embedding_fn_name)(**input_tokens)
            if isinstance(outputs, torch.Tensor):
                return outputs.cpu().numpy()
            hidden_states = outputs.hidden_states

            embeds = self._compute_embedding(hidden_states, input_tokens)
            for doc, embed in zip(docs, embeds):
                doc.embedding = embed


class MyIndexer(Executor):
    """Simple indexer class """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._docs = DocumentArray()
        self.top_k = backend_top_k

    @requests(on="/index")
    def index(self, docs: "DocumentArray", **kwargs):
        self._docs.extend(docs)

    @requests(on="/search")
    def search(self, docs: "DocumentArray", **kwargs):
        a = np.stack(docs.get_attributes("embedding"))
        b = np.stack(self._docs.get_attributes("embedding"))
        q_emb = _ext_A(_norm(a))
        d_emb = _ext_B(_norm(b))
        dists = _cosine(q_emb, d_emb)
        idx, dist = self._get_sorted_top_k(dists, self.top_k)
        for _q, _ids, _dists in zip(docs, idx, dist):
            for _id, _dist in zip(_ids, _dists):
                d = Document(self._docs[int(_id)], copy=True)
                d.score.value = 1 - _dist
                _q.matches.append(d)

    @staticmethod
    def _get_sorted_top_k(
        dist: "np.array", top_k: int
    ) -> Tuple["np.ndarray", "np.ndarray"]:
        if top_k >= dist.shape[1]:
            idx = dist.argsort(axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx, axis=1)
        else:
            idx_ps = dist.argpartition(kth=top_k, axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx_ps, axis=1)
            idx_fs = dist.argsort(axis=1)
            idx = np.take_along_axis(idx_ps, idx_fs, axis=1)
            dist = np.take_along_axis(dist, idx_fs, axis=1)

        return idx, dist


def _get_ones(x, y):
    return np.ones((x, y))


def _ext_A(A):
    nA, dim = A.shape
    A_ext = _get_ones(nA, dim * 3)
    A_ext[:, dim : 2 * dim] = A
    A_ext[:, 2 * dim :] = A ** 2
    return A_ext


def _ext_B(B):
    nB, dim = B.shape
    B_ext = _get_ones(dim * 3, nB)
    B_ext[:dim] = (B ** 2).T
    B_ext[dim : 2 * dim] = -2.0 * B.T
    del B
    return B_ext


def _euclidean(A_ext, B_ext):
    sqdist = A_ext.dot(B_ext).clip(min=0)
    return np.sqrt(sqdist)


def _norm(A):
    return A / np.linalg.norm(A, ord=2, axis=1, keepdims=True)


def _cosine(A_norm_ext, B_norm_ext):
    return A_norm_ext.dot(B_norm_ext).clip(min=0) / 2



class EmbeddingIndexer(Executor):
    def __init__(self, index_file_name: str, **kwargs):
        super().__init__(**kwargs)
        self.index_file_name = index_file_name
        if os.path.exists(self.save_path):
            self._docs = DocumentArray.load(self.save_path)
        else:
            self._docs = DocumentArray()

    @property
    def save_path(self):
        if not os.path.exists(self.workspace):
            os.makedirs(self.workspace)
        return os.path.join(self.workspace, self.index_file_name)

    def close(self):
        self._docs.save(self.save_path)

    @requests(on='/index')
    def index(self, docs: 'DocumentArray', **kwargs) -> DocumentArray:
        embedding_docs = DocumentArray()
        for doc in docs:
            embedding_docs.append(Document(id=doc.id, embedding=doc.embedding))
        self._docs.extend(embedding_docs)
        return docs

    @requests(on='/search')
    def search(self, docs: 'DocumentArray', parameters: Dict, **kwargs) \
            -> DocumentArray:
        a = np.stack(docs.get_attributes('embedding'))
        b = np.stack(self._docs.get_attributes('embedding'))
        q_emb = _ext_A(_norm(a))
        d_emb = _ext_B(_norm(b))
        dists = _cosine(q_emb, d_emb)
        top_k = int(parameters.get('top_k', 5))
        assert top_k > 0
        idx, dist = self._get_sorted_top_k(dists, top_k)
        for _q, _ids, _dists in zip(docs, idx, dist):
            for _id, _dist in zip(_ids, _dists):
                doc = Document(self._docs[int(_id)], copy=True)
                doc.score.value = 1 - _dist
                doc.parent_id = int(_id)
                _q.matches.append(doc)
        return docs

    @staticmethod
    def _get_sorted_top_k(
        dist: 'np.array', top_k: int
    ) -> Tuple['np.ndarray', 'np.ndarray']:
        if top_k >= dist.shape[1]:
            idx = dist.argsort(axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx, axis=1)
        else:
            idx_ps = dist.argpartition(kth=top_k, axis=1)[:, :top_k]
            dist = np.take_along_axis(dist, idx_ps, axis=1)
            idx_fs = dist.argsort(axis=1)
            idx = np.take_along_axis(idx_ps, idx_fs, axis=1)
            dist = np.take_along_axis(dist, idx_fs, axis=1)

        return idx, dist


class KeyValueIndexer(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if os.path.exists(self.save_path):
            self._docs = DocumentArray.load(self.save_path)
        else:
            self._docs = DocumentArray()

    @property
    def save_path(self):
        if not os.path.exists(self.workspace):
            os.makedirs(self.workspace)
        return os.path.join(self.workspace, 'kv.json')

    def close(self):
        self._docs.save(self.save_path)

    @requests(on='/index')
    def index(self, docs: DocumentArray, **kwargs) -> DocumentArray:
        self._docs.extend(docs)
        return docs

    @requests(on='/search')
    def query(self, docs: DocumentArray, **kwargs) -> DocumentArray:
        for doc in docs:
            for match in doc.matches:
                extracted_doc = self._docs[int(match.parent_id)]
                # The id fields should be the same
                assert match.id == extracted_doc.id
                match.MergeFrom(extracted_doc)
        return docs


