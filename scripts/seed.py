#!/usr/bin/env python3
"""
agent-mesh seed — ingesta inicial del conocimiento de Hermes al grafo.

Lee:
  - memories/MEMORY.md y memories/USER.md (perfil de Hermes)
  - skills/**/SKILL.md  (skills de Hermes)

…y lo sube al knowledge graph vía el servicio (client). Así cualquier agente
empieza con la misma base.

Uso:
    python scripts/seed.py --base http://127.0.0.1:8084 \
        --memories ~/.hermes/profiles/ruben/memories \
        --skills ~/.hermes/profiles/ruben/skills
Token: env KB_TOKEN (o --token).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from client import Client  # noqa: E402

def _slug(text: str, n: int = 42) -> str:
    s = re.sub(r"\W+", " ", text).strip(" ")
    return (s[:n].strip() + ("…" if len(s) > n else "")) or "?"

def chunks_from_md(path: Path) -> list[tuple[str, str]]:
    """Divide un MEMORY/USER .md en fragmentos (separa por '§' o por párrafos)."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    parts = re.split(r"\s*§\s*|\n\s*\n", raw)
    out = []
    for p in parts:
        p = p.strip()
        if p and len(p) > 4:
            out.append((_slug(p), p))
    return out

def chunks_from_skills(skills_dir: Path) -> list[tuple[str, str, dict]]:
    """Devuelve (name, desc+path, props) por cada SKILL.md encontrado."""
    out = []
    if not skills_dir.exists():
        return out
    for sk in sorted(skills_dir.glob("**/SKILL.md")):
        try:
            txt = sk.read_text(encoding="utf-8")
        except Exception:
            continue
        name = sk.parent.name
        m = re.search(r"^description:\s*(.+)$", txt, re.M)
        desc = (m.group(1).strip() if m else "")[:200]
        rel = sk.parent.relative_to(skills_dir).as_posix()
        out.append((name, f"{desc}\n({rel})", {"path": rel}))
    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("KB_BASE", "http://127.0.0.1:8084"))
    ap.add_argument("--token", default=os.environ.get("KB_TOKEN", ""))
    ap.add_argument("--memories", default="~/.hermes/profiles/ruben/memories")
    ap.add_argument("--skills", default="~/.hermes/profiles/ruben/skills")
    args = ap.parse_args()

    kb = Client(args.base, args.token)
    print(f"→ service: {kb.base}  health: {kb.health()}")

    mem_dir = Path(args.memories).expanduser()
    skills_dir = Path(args.skills).expanduser()

    # Agente / persona raíz
    hermes = kb.upsert_node("Agente", "Hermes",
                            "Asistente Hermes (Nous Research) del perfil ruben.")
    ruben = kb.upsert_node("Persona", "Rubén",
                           "usuario — gestor del homelab; español nativo.")
    kb.upsert_edge("USADO_POR", hermes["id"], ruben["id"])

    n_nota = n_skill = n_edge = 0
    for label, fname in (("Memoria", "MEMORY.md"), ("Preferencia", "USER.md")):
        for name, text in chunks_from_md(mem_dir / fname):
            nd = kb.upsert_node(label, name, text=text)
            kb.upsert_edge("BASADO_EN", nd["id"], hermes["id"])
            n_nota += 1

    for name, text, props in chunks_from_skills(skills_dir):
        nd = kb.upsert_node("Skill", name, text=text, properties=props)
        kb.upsert_edge("USADO_POR", nd["id"], hermes["id"])
        n_skill += 1

    print(f"✓ ingestados: {n_nota} notas mem/pref, {n_skill} skills, + edges.")
    print(f"  total en servicio: {kb.health()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
