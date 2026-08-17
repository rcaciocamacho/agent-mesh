"""agent-mesh — embeddings puros en Python (sin dependencias externas).

Fase 2 de similaridad semántica. Mientras TrueNAS no tenga el paquete
`postgresql-17-pgvector`, usamos hashing-trick sobre n-gramas de caracteres
(2..3 + la palabra completa) proyectados a un vector denso de `DIM`
dimensiones y normalizado.

Da similaridad por COSENO real (determinista, offline, sin API externa ni
modelos pesados) — suficiente para ~centenas/miles de nodos. Cuando el corpus
crezca, se migra a la extensión pgvector nativa (misma dimensión, operador
`<=>` en SQL) sin tocar a los clientes.

Config: `AGM_EMBED_DIM` (por defecto 192).
"""
from __future__ import annotations

import hashlib
import math
import os
import re

DIM = int(os.environ.get("AGM_EMBED_DIM", "192"))

_TOKEN = re.compile(r"[a-záéíóúüñ0-9]+")

def _hash(feat: str) -> int:
    return int(hashlib.md5(("agm1:" + feat).encode("utf-8")).hexdigest()[:8], 16)

def embed(text: str, dim: int = DIM) -> list[float]:
    """Vector denso normalizado (determinista) para un texto corto."""
    vec = [0.0] * dim
    seen = set()
    for t in _TOKEN.findall((text or "").lower()):
        if len(t) < 2:
            continue
        feats = {t}
        for ng in (2, 3):
            if len(t) > ng:
                feats.update(t[i:i + ng] for i in range(len(t) - ng + 1))
        for f in feats:
            if f in seen:
                continue
            seen.add(f)
            vec[_hash(f) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [round(x / norm, 6) for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    """Similaridad coseno entre dos vectores normalizados."""
    return sum(x * y for x, y in zip(a, b))
