"""TF-IDF fallback embedder (zero external dependencies)."""

import math
import re

STOPWORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'but', 'by',
    'for', 'from', 'had', 'has', 'have', 'he', 'her', 'his', 'how',
    'i', 'if', 'in', 'is', 'it', 'its', 'just', 'me', 'more', 'most',
    'my', 'no', 'of', 'on', 'or', 'other', 'out', 'over', 'same',
    'she', 'so', 'some', 'such', 'than', 'that', 'the', 'their',
    'them', 'then', 'there', 'these', 'they', 'this', 'to', 'too',
    'under', 'up', 'was', 'we', 'were', 'what', 'when', 'where',
    'which', 'who', 'why', 'will', 'with', 'you', 'your'
}


class TFIDFEmbedder:
    """TF-IDF embedder without external dependencies."""

    def __init__(self, max_features=500):
        """Initialize TF-IDF embedder.

        Args:
            max_features: Maximum vocabulary size
        """
        self.max_features = max_features
        self.vocab = {}  # word -> index
        self.idf = {}  # word -> idf value
        self.fitted = False
        self._available = True

    def _tokenize(self, text):
        """Tokenize text into lowercase words.

        Args:
            text: Input text string

        Returns:
            list: List of tokens
        """
        # Convert to lowercase
        text = text.lower()
        # Split on non-alphanumeric characters
        tokens = re.findall(r'\b[a-z]+\b', text)
        # Remove stopwords
        tokens = [t for t in tokens if t not in STOPWORDS]
        return tokens

    def fit(self, texts):
        """Fit TF-IDF model on texts.

        Args:
            texts: List of text strings
        """
        # Count document frequency for each word
        doc_freq = {}
        all_tokens = set()

        for text in texts:
            tokens = set(self._tokenize(text))
            all_tokens.update(tokens)
            for token in tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        # Sort by document frequency and select top max_features
        sorted_tokens = sorted(doc_freq.items(), key=lambda x: x[1], reverse=True)
        top_tokens = [token for token, _ in sorted_tokens[:self.max_features]]

        # Create vocabulary
        self.vocab = {token: idx for idx, token in enumerate(top_tokens)}

        # Compute IDF values
        num_docs = len(texts)
        for token in top_tokens:
            df = doc_freq[token]
            self.idf[token] = math.log(num_docs / df) if df > 0 else 0.0

        self.fitted = True

    def _compute_tfidf_vector(self, text):
        """Compute TF-IDF vector for a text.

        Args:
            text: Input text string

        Returns:
            list: TF-IDF vector of length max_features
        """
        vector = [0.0] * self.max_features

        tokens = self._tokenize(text)

        # Count term frequency
        term_freq = {}
        for token in tokens:
            term_freq[token] = term_freq.get(token, 0) + 1

        # Normalize TF by document length
        doc_length = len(tokens)
        if doc_length == 0:
            return vector

        # Fill vector with TF-IDF values
        for token, tf in term_freq.items():
            if token in self.vocab:
                idx = self.vocab[token]
                idf = self.idf.get(token, 0.0)
                vector[idx] = (tf / doc_length) * idf

        return vector

    def embed(self, text):
        """Embed a single text.

        Args:
            text: Input text string

        Returns:
            list: Embedding vector of length max_features
        """
        if not self.fitted:
            self.fit([text])

        return self._compute_tfidf_vector(text)

    def embed_batch(self, texts):
        """Embed multiple texts.

        Args:
            texts: List of text strings

        Returns:
            list[list[float]]: List of embedding vectors
        """
        if not self.fitted:
            self.fit(texts)

        return [self._compute_tfidf_vector(text) for text in texts]

    @property
    def _fitted(self):
        return self.fitted

    @property
    def available(self):
        return self._available

    @property
    def dim(self):
        """Get embedding dimension.

        Returns:
            int: Dimension of embeddings
        """
        return self.max_features
