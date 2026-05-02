"""Shared math utilities for embeddings."""

import math


def _to_list(x):
    """Convert numpy array to list if needed, otherwise return as-is."""
    try:
        return x.tolist() if hasattr(x, 'tolist') else list(x)
    except (TypeError, AttributeError):
        return list(x)


def l2_norm(x):
    """Compute L2 norm of a vector.

    Args:
        x: list or numpy array

    Returns:
        float: L2 norm
    """
    x = _to_list(x)
    return math.sqrt(sum(val ** 2 for val in x))


def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors.

    Args:
        a: list or numpy array
        b: list or numpy array

    Returns:
        float: cosine similarity in range [-1, 1], or 0 if either vector is zero
    """
    a = _to_list(a)
    b = _to_list(b)

    # Compute dot product
    dot_product = sum(x * y for x, y in zip(a, b, strict=False))

    # Compute norms
    norm_a = l2_norm(a)
    norm_b = l2_norm(b)

    # Handle zero vectors
    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (norm_a * norm_b)


def cosine_similarity_matrix(embeddings):
    """Compute pairwise cosine similarity matrix.

    Args:
        embeddings: list of vectors (each vector is a list or numpy array)

    Returns:
        list[list[float]]: 2D list of pairwise cosine similarities
    """
    n = len(embeddings)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(i, n):
            sim = cosine_similarity(embeddings[i], embeddings[j])
            matrix[i][j] = sim
            matrix[j][i] = sim

    return matrix


def hoyer_sparsity(x):
    """Compute Hoyer sparsity measure.

    Hoyer sparsity = (sqrt(n) - L1/L2) / (sqrt(n) - 1)

    Args:
        x: list or numpy array

    Returns:
        float: sparsity in range [0, 1], or 0 if vector is zero
    """
    x = _to_list(x)

    n = len(x)
    if n == 0:
        return 0.0

    # Compute L1 norm (sum of absolute values)
    l1 = sum(abs(val) for val in x)

    # Compute L2 norm
    l2 = l2_norm(x)

    # Handle zero vector
    if l2 == 0:
        return 0.0

    # Avoid division by zero when n=1
    if n == 1:
        return 0.0

    sqrt_n = math.sqrt(n)
    return (sqrt_n - l1 / l2) / (sqrt_n - 1)


def difference_vector(a, b):
    """Compute element-wise difference vector b - a.

    Args:
        a: list or numpy array
        b: list or numpy array

    Returns:
        list: element-wise differences
    """
    a = _to_list(a)
    b = _to_list(b)

    if len(a) != len(b):
        raise ValueError(f"Vector dimensions must match: {len(a)} vs {len(b)}")

    return [y - x for x, y in zip(a, b, strict=True)]
