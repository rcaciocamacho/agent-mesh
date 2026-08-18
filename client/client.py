"""
agent-mesh client — acceso al knowledge graph compartido.

Stdlib puro (urllib) para que cualquier agente (Hermes local/rack, Mako,
JARVIS) lo use sin instalar dependencias. Apunta al servicio del rack.

Uso:
    from client import Client
    kb = Client("http://rack:8084", token="...")
    kb.upsert_node("Concepto", "data", text="...", properties={...})
    kb.upsert_edge("RELACIONADO_CON", s, t)
    kb.search("presencia jarvis")
    kb.graph(node_id)

Failover (F4 spec 02): base_url acepta UNA url o VARIAS separadas por coma
    kb = Client("http://192.168.0.45:8084,http://192.168.0.26:18085", token="...")
Cada petición prueba las bases EN ORDEN y conmuta a la siguiente solo ante
errores de CONEXIÓN (red caída, timeout, servidor apagado). Las respuestas
HTTP válidas (incluido 401/404/409) NO disparan failover: el servicio está
vivo, el error es de la petición. Si todas las bases fallan, se lanza el
error de la primera.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

class Client:
    def __init__(self, base_url: str | list[str], token: str = "",
                 timeout: float = 10.0):
        # acepta string con una URL, string con comas (varias), o lista
        if isinstance(base_url, str):
            bases = [b.strip() for b in base_url.split(",") if b.strip()]
        else:
            bases = list(base_url)
        if not bases:
            raise ValueError("base_url vacío")
        self.bases = [b.rstrip("/") for b in bases]
        self.token = token
        self.timeout = timeout

    # ── bajo nivel ─────────────────────────────────────────────────────────
    def _req(self, method: str, path: str, payload=None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        first_err: Exception | None = None
        for base in self.bases:
            url = f"{base}{path}"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                # respuesta HTTP válida (401/404/409/...): el servicio está
                # vivo → NO conmutar, devolver el error de esta base
                body = e.read().decode("utf-8", "ignore")
                raise RuntimeError(f"HTTP {e.code} en {method} {path}: {body}") from e
            except Exception as e:
                # error de conexión → intentar la siguiente base
                if first_err is None:
                    first_err = e
                continue
        # todas las bases fallaron por conexión
        raise RuntimeError(
            f"sin conexión a agent-mesh ({len(self.bases)} base(s)): {first_err}"
        ) from first_err

    # ── API pública ────────────────────────────────────────────────────────
    def health(self) -> dict:
        return self._req("GET", "/health")

    def schema(self) -> dict:
        return self._req("GET", "/schema")

    def upsert_node(self, label: str, name: str, text: str = "",
                    properties: dict | None = None) -> dict:
        import urllib.parse
        path = f"/n/{urllib.parse.quote(label)}/{urllib.parse.quote(name)}"
        return self._req("PUT", path, {
            "label": label, "name": name, "text": text,
            "properties": properties or {}})

    def delete_node(self, label: str, name: str) -> dict:
        import urllib.parse
        path = f"/n/{urllib.parse.quote(label)}/{urllib.parse.quote(name)}"
        return self._req("DELETE", path)

    def upsert_edge(self, etype: str, source: int, target: int,
                    properties: dict | None = None) -> dict:
        import urllib.parse
        return self._req("PUT", f"/e/{urllib.parse.quote(etype)}", {
            "type": etype, "source": source, "target": target,
            "properties": properties or {}})

    def search(self, q: str, limit: int = 10) -> list[dict]:
        import urllib.parse
        resp = self._req("GET", f"/search?q={urllib.parse.quote(q)}&limit={limit}")
        return resp.get("results", [])

    def similar(self, text: str, limit: int = 10) -> list[dict]:
        resp = self._req("POST", f"/similar?limit={limit}", {"text": text})
        return resp.get("results", [])

    def graph(self, node_id: int, hops: int = 1) -> dict:
        return self._req("POST", f"/graph?node={node_id}&hops={hops}")
