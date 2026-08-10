"""Local text embeddings for clustering (replaces per-batch LLM clustering's
implicit "does this song fit near that song" judgment with an explicit
vector space -- see library_analysis.py's embed-then-cluster pipeline).

Model weights are pre-downloaded at Docker build time (see backend/Dockerfile)
so the first real request never pays a cold HuggingFace Hub fetch. The
sentence_transformers import itself is deferred into `_load_model` rather than
sitting at module level -- it drags in torch, which is slow to import and
unnecessary in every test process that only ever calls the mocked
`embed_texts` (mirrors the litellm/completion import already sitting at
module level in library_analysis.py, except here the weight of the import
justifies deferring it instead).
"""

from functools import lru_cache

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


@lru_cache
def _load_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Returns one L2-normalized embedding vector per input text, same order.

    Normalized so callers can cluster with plain Euclidean distance and get
    cosine-equivalent results (‖a-b‖² = 2 - 2·cos(a,b) for unit vectors) --
    scikit-learn's HDBSCAN doesn't accept a "cosine" metric directly, so
    normalizing up front avoids needing a precomputed distance matrix.
    """
    if not texts:
        return []
    model = _load_model()
    return model.encode(texts, normalize_embeddings=True, convert_to_numpy=True).tolist()
