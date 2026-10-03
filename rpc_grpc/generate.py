"""Génère les stubs client et squelettes serveur à partir des contrats IDL.

    python -m rpc_grpc.generate

Équivaut à lancer le compilateur ``protoc`` avec le plugin gRPC Python :

    protoc -I rpc_grpc/protos --python_out=... --grpc_python_out=... service.proto
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROTO_DIR = Path(__file__).resolve().parent / "protos"
OUT_DIR = Path(__file__).resolve().parent / "generated"
PROTO_FILES = ("service.proto", "service_v2.proto")


def generate(verbose: bool = True) -> list[Path]:
    """Compile les .proto ; renvoie la liste des fichiers produits."""
    from grpc_tools import protoc
    from importlib import resources

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "__init__.py").write_text(
        '"""Code généré par protoc — ne pas modifier à la main (voir rpc_grpc/generate.py)."""\n',
        encoding="utf-8",
    )
    well_known = resources.files("grpc_tools") / "_proto"
    for proto in PROTO_FILES:
        code = protoc.main([
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"-I{well_known}",
            f"--python_out={OUT_DIR}",
            f"--pyi_out={OUT_DIR}",
            f"--grpc_python_out={OUT_DIR}",
            str(PROTO_DIR / proto),
        ])
        if code != 0:
            raise RuntimeError(f"protoc a échoué sur {proto} (code {code})")

    # protoc émet « import service_pb2 » (import absolu) : on le rend relatif au paquet.
    for grpc_file in OUT_DIR.glob("*_pb2_grpc.py"):
        source = grpc_file.read_text(encoding="utf-8")
        patched = re.sub(r"^import (\w+_pb2) as", r"from . import \1 as", source, flags=re.MULTILINE)
        grpc_file.write_text(patched, encoding="utf-8")

    produced = sorted(p for p in OUT_DIR.iterdir() if p.name != "__init__.py" and p.is_file())
    if verbose:
        for path in produced:
            print(f"  généré : {path.relative_to(OUT_DIR.parent.parent)}")
    return produced


def ensure_generated() -> None:
    """Régénère le code si un .proto est plus récent que sa sortie (ou si elle manque)."""
    for proto in PROTO_FILES:
        source = PROTO_DIR / proto
        target = OUT_DIR / f"{source.stem}_pb2_grpc.py"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            generate(verbose=False)
            return


if __name__ == "__main__":
    print("Compilation des contrats IDL avec protoc…")
    generate()
    sys.exit(0)
