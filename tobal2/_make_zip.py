# 配布用 zip（tobal2_jkguard.zip）を作り直す。ツール本体か waza/ を直したら実行する。
#   python3 _make_zip.py
# 先頭が _ のファイルは GitHub Pages で公開されない。
import zipfile
from pathlib import Path

here = Path(__file__).resolve().parent
out = here / "tobal2_jkguard.zip"
files = [here / "TOBAL2_duck_JKguard.py"] + sorted((here / "waza").glob("*.json"))

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f, "tobal2/" + f.relative_to(here).as_posix())
print(f"{out.name}: {len(files)} ファイル")
