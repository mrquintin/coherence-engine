"""Contradiction-direction (ĉ) fitting and scoring.

Given pairs of sentence embeddings ``(u, v)`` labeled as contradictions,
the per-pair Householder reflection axis is::

    n_i = (u_i - v_i) / ||u_i - v_i||

The aggregate contradiction direction ``ĉ`` is the leading principal
direction of the set ``{n_i}`` — the axis along which contradiction
pairs maximally disagree, ignoring sign. See
``apps/site/src/content/research/contradiction_direction.mdx`` for the
research framing.

This module is the canonical, callable surface for the engine and the
stability study (Wave 13, prompt 48). It deliberately keeps three small
exported functions so callers can compose them without re-deriving the
geometry: ``fit_c_hat``, ``project``, and ``abs_cosine``.

ĉ is only defined up to sign. ``fit_c_hat`` fixes the sign
deterministically (first non-zero coordinate is positive) so that two
runs over the same data produce byte-identical vectors.
"""
from __future__ import annotations


import numpy as np

__all__ = [
    "fit_c_hat",
    "pair_directions",
    "project",
    "cosine",
    "abs_cosine",
]


def _as_pairs(pairs) -> np.ndarray:
    arr = np.asarray(pairs, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[1] != 2:
        raise ValueError(
            f"pairs must have shape (n_pairs, 2, dim); got shape {arr.shape}"
        )
    if arr.shape[0] == 0:
        raise ValueError("pairs must contain at least one (u, v) pair")
    return arr


def _safe_normalize(x: np.ndarray, axis: int = -1, eps: float = 1e-12) -> np.ndarray:
    n = np.linalg.norm(x, axis=axis, keepdims=True)
    return x / np.maximum(n, eps)


def _canonical_sign(c: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    """Pick the sign so the first non-zero coordinate is positive."""
    flat = c.ravel()
    for x in flat:
        if x > eps:
            return c
        if x < -eps:
            return -c
    return c


def pair_directions(pairs) -> np.ndarray:
    """Per-pair Householder normal ``n_i = (u_i - v_i) / ||u_i - v_i||``.

    Returns an array of shape ``(n_pairs, dim)``.
    """
    arr = _as_pairs(pairs)
    diff = arr[:, 0, :] - arr[:, 1, :]
    return _safe_normalize(diff, axis=1)


def fit_c_hat(pairs) -> np.ndarray:
    """Fit ĉ as the dominant principal direction of the pair normals.

    Parameters
    ----------
    pairs : array-like of shape ``(n_pairs, 2, dim)``
        Each entry is a ``(u, v)`` pair of contradiction embeddings.

    Returns
    -------
    np.ndarray, shape ``(dim,)``
        Unit-norm contradiction direction with deterministic sign.
    """
    N = pair_directions(pairs)  # (n_pairs, dim)
    if N.shape[0] == 1:
        return _canonical_sign(N[0])
    # The leading right singular vector of N maximises Σ_i (n_i · c)^2 over
    # unit c — exactly the principal-axis criterion described in the docs.
    _, _, Vt = np.linalg.svd(N, full_matrices=False)
    c = Vt[0]
    c = c / max(float(np.linalg.norm(c)), 1e-12)
    return _canonical_sign(c)


def project(pairs, c_hat) -> np.ndarray:
    """``|⟨u - v, ĉ⟩|`` per pair — the discriminator score for AUC.

    The sign is dropped because ĉ is only defined up to sign; downstream
    ROC comparisons should use the magnitude.
    """
    arr = _as_pairs(pairs)
    c = np.asarray(c_hat, dtype=np.float64).ravel()
    if c.shape[0] != arr.shape[2]:
        raise ValueError(
            f"c_hat dim {c.shape[0]} != pair dim {arr.shape[2]}"
        )
    d = arr[:, 0, :] - arr[:, 1, :]
    return np.abs(d @ c)


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def abs_cosine(a, b) -> float:
    """Sign-invariant cosine — the right metric for comparing ĉ vectors."""
    return abs(cosine(a, b))
