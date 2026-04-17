"""Sentence-BERT embedder (requires sentence-transformers)."""


class SBERTEmbedder:
    """Sentence-BERT embedder using sentence-transformers library."""

    def __init__(self, model_name="all-mpnet-base-v2", device="auto",
                 batch_size=32):
        """Initialize SBERT embedder.

        Args:
            model_name: HuggingFace model name
            device: Device to use ('cpu', 'cuda', 'mps', 'auto')
            batch_size: Default batch size for embed_batch
        """
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self.model = None
        self._available = False

        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name, device=device)
            self._available = True
        except ImportError:
            self._available = False
        except Exception:
            self._available = False

    def embed(self, text):
        """Embed a single text.

        Args:
            text: Input text string

        Returns:
            list: Embedding vector

        Raises:
            RuntimeError: If embedder is not available
        """
        if not self._available or self.model is None:
            raise RuntimeError("SentenceTransformer not available")

        embedding = self.model.encode(text)
        return embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

    def embed_batch(self, texts, batch_size=None):
        """Embed multiple texts.

        Args:
            texts: List of text strings
            batch_size: Batch size for encoding (uses instance default if None)

        Returns:
            list[list[float]]: List of embedding vectors

        Raises:
            RuntimeError: If embedder is not available
        """
        if not self._available or self.model is None:
            raise RuntimeError("SentenceTransformer not available")

        if batch_size is None:
            batch_size = self.batch_size
        embeddings = self.model.encode(texts, batch_size=batch_size)
        # Convert numpy array to list of lists
        if hasattr(embeddings, 'tolist'):
            return embeddings.tolist()
        return [list(e) for e in embeddings]

    @property
    def dim(self):
        """Get embedding dimension.

        Returns:
            int: Dimension of embeddings (768 for all-mpnet-base-v2)
        """
        if self.model is None:
            return 768  # Default for all-mpnet-base-v2

        # Try to get dimension from model config
        try:
            return self.model.get_sentence_embedding_dimension()
        except Exception:
            return 768

    @property
    def available(self):
        """Check if embedder is available.

        Returns:
            bool: True if SentenceTransformer is available and loaded
        """
        return self._available
