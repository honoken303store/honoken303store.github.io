# TOBAL2 JKガード監視（DuckStation用）
# 攻撃パターン表（0x12806C〜、10枠: 枠0・1=その敵だけのメモ、枠2〜9=記憶ノート）を
# DuckStationのGDBサーバー経由で小窓に表示し、増えた枠と直前に出していた技をログに残す。
# 書き換えテスト用に、枠への書き込みと「枠0・1を空にし続ける」機能も付けている。
#
# 準備: DuckStation 設定 → デバッグ → 「GDB サーバーを有効化」をオン（ポート2345）
# 起動: Windows は  py TOBAL2_duck_JKguard.py
#       Mac/Linux は python3 TOBAL2_duck_JKguard.py
# ログ: このファイルと同じ場所の logs フォルダに保存される
# 注意: GDBサーバーには1本しか繋がらない想定なので、他のGDBツール（TOBAL2_duck_RNG.py など）とは同時に使わない

import json
import re
import socket
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox
from collections import deque
from datetime import datetime
from pathlib import Path

HOST = "127.0.0.1"
PORT = 2345
RAM = 0x80000000               # PS1メモリマップのメインRAM先頭
READ_CHUNK = 0x400             # 1パケットで読むバイト数の上限
PACKET_RE = re.compile(rb"\$([^#]*)#[0-9a-fA-F]{2}")

TABLE_TOP = RAM + 0x128060     # タイマー的な値（u16）〜表の終わりまでまとめて読む
TABLE_SIZE = 0x34
SLOT_OFFSET = 0x0C             # 0x12806C が枠0
SLOT_COUNT = 10
CURRENT_SLOTS = 2              # 枠0・1 = その敵だけのメモ（敵が替わると空になる）
GROUP_ADDR = RAM + 0x1256A9    # 技グループ値（JK等=30、中=1、スライディング=0）
ENEMY_HP_ADDR = RAM + 0x1260B4 # 敵HP（u16）
AREA_ADDR = RAM + 0x123108     # エリアID（0x1C=封印の迷宮）
FLOOR_ADDR = RAM + 0x12311B
PLAYTIME_ADDR = RAM + 0x0F8BD9 # 秒・分・時
MOVE_ADDR = RAM + 0x123DE8     # 今の技ID（u16、表のIDと同じ形式。+2にも同じ値）
LAST_HIT_ADDR = RAM + 0x1249E0 # 最後に当てた技ID？（u16、ガードされると0の可能性）
ENEMY_MOVE_ADDR = RAM + 0x12602C  # 敵の今の技ID（u16、自分の0x123DE8と同じ並び。+2にも同じ値）
ENEMY_NAME_ID_ADDR = RAM + 0x126015  # 敵の名前番号（u8、自分側の0x123DD1にはキャラ番号 エポン=1/チャコ=9）
NAME_TABLE_ADDR = RAM + 0x0D0798     # 名前ポインタ表（u32×番号、MAP.luaと同じ）
NAME_MAX = 24
# 敵が攻撃をガードした時の動作（立ち: 0037→013C、しゃがみ: 0048→003E→008D）
ENEMY_GUARD_MOVES = {0x013C: "立ちガード", 0x008D: "しゃがみガード"}

POLL_MS = 16
RETRY_MS = 1000
FLASH_SEC = 1.5          # 回数が増えた枠を色付けする時間
MOVE_HISTORY_SEC = 3.0   # ログに添える「直前に出していた技」の範囲
IDLE_MOVES = {0x0003, 0x0008, 0x0009}  # 立ち・前歩き・横移動は「直前の技」に含めない
LOG_LINES = 300
LOAD_SLOT_CHANGES = 3    # 表の枠がこれ以上同時に変わったらステートロードとみなす
LOG_DIR = Path(__file__).resolve().parent / "logs"

WAZA_DIR = Path(__file__).resolve().parent / "waza"  # 技表（キャラごとのJSON）
ATTEMPT_GAP_SEC = 0.7    # 技登録: キャラの技IDがこれ以上途切れたら、次に出た技を新しい1回分として数え直す
IDLE_HOLD_SEC = 1.5      # 技登録: 同じIDがこれ以上続いたら待機の動作とみなす（キャラ専用の待機IDがあるため）

# 技表から作る ID → (技名, コマンド)。起動時と技登録の後に waza フォルダから読み直す
KNOWN_IDS = {}
CHARACTERS = {0x08: "エポン", 0x10: "マリー", 0x16: "チャコ"}
# エリアID（0x123108）→ダンジョン名（分かったら追記していく）
AREA_NAMES = {0x21: "プラクティス", 0x1E: "遺跡", 0x26: "神殿", 0x1C: "封印の迷宮", 0x2F: "フィールド"}
LABEL_MODES = ("技名", "コマンド")


def u16(data, offset):
    return int.from_bytes(data[offset:offset + 2], "little")


label_mode = 0  # 0=技名、1=コマンド（ボタンで切り替え）


def id_label(slot_id):
    if slot_id in KNOWN_IDS:
        return KNOWN_IDS[slot_id][label_mode]
    return CHARACTERS.get(slot_id >> 8, "共通?" if slot_id >> 8 == 0 else "")


def load_waza():
    """waza フォルダのJSONを読む。戻り値は ({ファイル: 中身}, [読めなかったファイルのメッセージ])"""
    files, errors = {}, []
    for path in sorted(WAZA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for move in data["技"]:
                [int(i, 16) for i in move["ID"]]
            [int(i, 16) for i in data.get("待機ID", [])]
            [int(i, 16) for i in data.get("表示名", {})]
            files[path] = data
        except (OSError, ValueError, KeyError, TypeError) as e:
            errors.append(f"{path.name} が読めません: {e}")
    return files, errors


def build_known_ids(files):
    """技の最後の段のIDはその技の名前、途中の段のIDは「技名(n段目)」にする。
    同じIDを使う技が複数ある時は、そのIDが出るまでの段の数が少ない技を優先する（連携技の最後の段が単発技の名前を取らないように）。
    段の数が同じなら、そのIDで終わる技 → 技表で先に書いてある技の順"""
    best = {}  # ID → ((段, 途中の段なら1), (技名, コマンド))
    for data in files.values():
        prefix = int(data["ID上2桁"] or "0", 16)  # 空 = まだ分からない
        if prefix:
            CHARACTERS[prefix] = data["キャラ"]
        for i in data.get("待機ID", []):
            best.setdefault(int(i, 16), ((0, 0), ("待機", "待機")))
        for move in data["技"]:
            ids = [int(i, 16) for i in move["ID"]]
            parts = move["コマンド"].split("､")
            for n, slot_id in enumerate(ids, 1):
                if n == len(ids):
                    key, label = (n, 0), (move["技名"], move["コマンド"])
                else:
                    cmd = "､".join(parts[:n]) if len(parts) == len(ids) else move["コマンド"]
                    key, label = (n, 1), (f"{move['技名']}({n}段目)", cmd)
                if slot_id not in best or key < best[slot_id][0]:
                    best[slot_id] = (key, label)
    KNOWN_IDS.clear()
    KNOWN_IDS.update({slot_id: label for slot_id, (_, label) in best.items()})
    # 技表の "表示名" は上の決め方より優先（同じIDを使う技どうしで、表示を手で決めたい時）
    for data in files.values():
        for i, (name, cmd) in data.get("表示名", {}).items():
            KNOWN_IDS[int(i, 16)] = (name, cmd)


def save_waza(path, data):
    """手で直しやすいように1技1行で書く"""
    lines = ["{", f'  "キャラ": {json.dumps(data["キャラ"], ensure_ascii=False)},',
             f'  "ID上2桁": {json.dumps(data["ID上2桁"])},',
             *([f'  "待機ID": {json.dumps(data["待機ID"])},'] if data.get("待機ID") else []),
             *([f'  "表示名": {json.dumps(data["表示名"], ensure_ascii=False)},'] if data.get("表示名") else []),
             '  "技": [',
             ",\n".join("    " + json.dumps(m, ensure_ascii=False) for m in data["技"]),
             "  ]", "}", ""]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def gdb_packet(body):
    return f"${body}#{sum(body.encode()) % 256:02x}".encode()


class GdbReader:
    def __init__(self):
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=0.5)
        self.sock.settimeout(0.5)
        # 接続するとエミュレーションが一時停止するので、再開させる
        self.sock.sendall(gdb_packet("c"))

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _read_chunk(self, addr, size):
        self.sock.sendall(gdb_packet(f"m{addr:x},{size:x}"))
        buf = b""
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("切断されました")
            buf += chunk
            for m in PACKET_RE.finditer(buf):
                body = m.group(1)
                if body.startswith(b"E"):
                    self.sock.sendall(b"+")
                    raise ConnectionError(f"読み取りエラー: {body.decode()}")
                if len(body) == size * 2:
                    self.sock.sendall(b"+")
                    return bytes.fromhex(body.decode())

    def read(self, addr, size):
        """addrからsizeバイトを読み込み（大きい範囲は分割して読む）"""
        data = b""
        while len(data) < size:
            n = min(READ_CHUNK, size - len(data))
            data += self._read_chunk(addr + len(data), n)
        return data


class GdbClient(GdbReader):
    def write(self, addr, data):
        self.sock.sendall(gdb_packet(f"M{addr:x},{len(data):x}:{data.hex()}"))
        buf = b""
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("切断されました")
            buf += chunk
            for m in PACKET_RE.finditer(buf):
                body = m.group(1)
                if body == b"OK" or body.startswith(b"E"):
                    self.sock.sendall(b"+")
                    if body != b"OK":
                        raise ConnectionError(f"書き込みエラー: {body.decode()}")
                    return


class Register:
    """技登録ウィンドウ。技表の技を1つずつ表示し、実際に出した技IDの並びをその技に登録する"""

    def __init__(self, app):
        self.app = app
        self.chain = []         # 今回出た技IDの並び（同じIDの連続はまとめる）
        self.last_seen = 0.0    # キャラの技IDが最後に出ていた時刻
        self.cur_id, self.cur_since = None, 0.0  # 今出ているIDと、出始めた時刻（待機の判定用）
        self.path = None
        self.index = 0

        self.top = tk.Toplevel(app.root)
        self.top.title("技登録")
        self.top.attributes("-topmost", True)
        self.top.configure(bg="black", padx=10, pady=8)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        head = tk.Frame(self.top, bg="black")
        head.pack(fill="x")
        self.paths = {path.stem: path for path in app.waza}
        self.file_var = tk.StringVar(value=next(iter(self.paths)))
        tk.OptionMenu(head, self.file_var, *self.paths, command=lambda _: self.select_file()).pack(side="left")
        self.progress = tk.Label(head, font=app.fonts[13], fg="gray60", bg="black")
        self.progress.pack(side="left", padx=8)

        self.move_label = tk.Label(self.top, font=app.fonts[16], fg="white", bg="black", justify="left", anchor="w")
        self.move_label.pack(fill="x", pady=(8, 0))
        self.saved_label = tk.Label(self.top, font=app.fonts[13], fg="gray60", bg="black", justify="left", anchor="w")
        self.saved_label.pack(fill="x")
        self.idle_label = tk.Label(self.top, font=app.fonts[13], fg="gray60", bg="black", justify="left", anchor="w")
        self.idle_label.pack(fill="x")
        # 出たIDの並び。登録済みのIDは色を変えて、まだ無いIDと見分けやすくする
        self.chain_text = tk.Text(self.top, font=app.fonts[16], fg="#9fe89f", bg="black", width=44, height=1,
                                  borderwidth=0, highlightthickness=0, state="disabled", cursor="arrow")
        self.chain_text.tag_configure("new", foreground="#9fe89f")    # 未登録
        self.chain_text.tag_configure("this", foreground="#7fd4ff")   # 今の技に登録済み
        self.chain_text.tag_configure("other", foreground="#ffd84d")  # 他の技（待機）で登録済み
        self.chain_text.tag_configure("info", foreground="gray60")
        self.chain_text.pack(fill="x", pady=8)

        buttons = tk.Frame(self.top, bg="black")
        buttons.pack(fill="x")
        for text, command in (("登録 (Enter)", self.register), ("スキップ (S)", self.skip),
                              ("やり直し (BS)", self.retry),
                              ("← 前", lambda: self.go(-1)), ("次 →", lambda: self.go(1)),
                              ("待機の判定を消す", self.clear_idle)):
            tk.Button(buttons, text=text, command=command).pack(side="left")

        # 手入力: wikiに無い技の追加や、IDを直接書き込む時に使う
        manual = tk.Frame(self.top, bg="black")
        manual.pack(fill="x", pady=(8, 0))
        self.m_ids, self.m_name, self.m_cmd = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, var, width in (("ID", self.m_ids, 16), ("技名", self.m_name, 14), ("コマンド", self.m_cmd, 10)):
            tk.Label(manual, text=label, fg="white", bg="black").pack(side="left")
            entry = tk.Entry(manual, textvariable=var, width=width)
            entry.pack(side="left", padx=(2, 8))
            if var is not self.m_cmd:
                entry.bind("<Return>", lambda e: self.find())  # ID欄・技名欄で Enter = 探す
        tk.Button(manual, text="探す", command=self.find).pack(side="left")
        # 何を変えるボタンか分かるよう、段に分けて見出しを付ける
        rows = (
            ("ノートの表示", (("ノートに出る名前を変更", self.set_label),)),
            ("技表", (("wikiに無い技を足す", self.add_move), ("この技を消す", self.delete_move),
                      ("このIDを待機にする", self.add_typed_idle))),
        )
        for title, row_buttons in rows:
            row = tk.Frame(self.top, bg="black")
            row.pack(fill="x", pady=(4, 0))
            tk.Label(row, text=title, font=self.app.fonts[13], fg="gray60", bg="black", width=12,
                     anchor="w").pack(side="left")
            for text, command in row_buttons:
                tk.Button(row, text=text, command=command).pack(side="left")

        # 入力欄で文字を打っている時はキー操作にしない
        for key, command in (("<Return>", self.register), ("<BackSpace>", self.retry), ("s", self.skip),
                             ("<Left>", lambda: self.go(-1)), ("<Right>", lambda: self.go(1))):
            self.top.bind(key, lambda e, c=command: None if isinstance(e.widget, tk.Entry) else c())
        self.select_file()

    def close(self):
        self.app.register_window = None
        self.top.destroy()

    @property
    def data(self):
        return self.app.waza[self.path]

    def select_file(self):
        self.path = self.paths[self.file_var.get()]
        self.prefix = int(self.data["ID上2桁"], 16) if self.data["ID上2桁"] else None
        # まだIDが無い（スキップもしていない）最初の技から始める
        self.index = next((i for i, m in enumerate(self.data["技"]) if self.todo(m)), 0)
        self.retry()

    @staticmethod
    def todo(move):
        return not move["ID"] and not move.get("スキップ")

    def go_next_todo(self):
        """次のまだやっていない技へ（無ければ1つ次へ）"""
        moves = self.data["技"]
        order = list(range(self.index + 1, len(moves))) + list(range(self.index + 1))
        self.index = next((i for i in order if self.todo(moves[i])), (self.index + 1) % len(moves))
        self.retry()

    def go(self, step):
        self.index = (self.index + step) % len(self.data["技"])
        self.retry()

    def retry(self):
        self.chain = []
        self.show()

    def register(self):
        if self.chain:
            self.set_ids(self.data["技"][self.index], self.chain)
            self.go_next_todo()

    def set_ids(self, move, ids):
        move["ID"] = [f"{i:04X}" for i in ids]
        if self.prefix is None:
            # ID上2桁が未確認のキャラは、最初に登録した技から決める
            self.prefix = ids[0] >> 8
            self.data["ID上2桁"] = f"{self.prefix:02X}"
            self.app.add_log(f"{self.data['キャラ']} のID上2桁を {self.prefix:02X} にしました", "mark")
        move.pop("スキップ", None)
        self.save(f"技登録 {move['技名']}（{move['コマンド']}） = {'→'.join(move['ID'])}")

    def typed_ids(self):
        """ID欄の16進（空白・→・カンマ区切り）を読む。読めない時は None"""
        parts = re.split(r"[\s→,､、]+", self.m_ids.get().strip())
        if not all(re.fullmatch(r"[0-9A-Fa-f]{1,4}", x) for x in parts):
            return None
        return [int(x, 16) for x in parts]

    def set_label(self):
        """ID欄のIDの表示名を、技名欄・コマンド欄の内容にする（技表の "表示名"）。技名欄が空なら元に戻す"""
        ids = self.typed_ids()
        if ids is None or len(ids) != 1:
            self.app.add_log("表示名を変えるIDを1つだけID欄に入れてください（例: 04B2）", "mark")
            return
        key, name = f"{ids[0]:04X}", self.m_name.get().strip()
        labels = self.data.setdefault("表示名", {})
        if name:
            cmd = self.m_cmd.get().strip() or KNOWN_IDS.get(ids[0], ("", ""))[1]
            labels[key] = [name, cmd]
            log_text = f"技登録 {key} の表示名を「{name}」（{cmd}）にしました"
        elif labels.pop(key, None):
            log_text = f"技登録 {key} の表示名を元に戻しました"
        else:
            self.app.add_log("技名欄に表示名を入れてください（空にすると元に戻します）", "mark")
            return
        if not labels:
            del self.data["表示名"]
        self.save(log_text)
        for var in (self.m_ids, self.m_name, self.m_cmd):
            var.set("")
        self.show()

    def add_move(self):
        """wikiに無い技を技表の最後に足す。IDはID欄、空ならその時に出したIDの並び"""
        name = self.m_name.get().strip()
        ids = self.typed_ids() if self.m_ids.get().strip() else self.chain
        if not name or not ids:
            self.app.add_log("新しい技には技名と、ID（ID欄に入れるか技を出す）が必要です", "mark")
            return
        move = {"分類": "技表に無い技", "技名": name, "コマンド": self.m_cmd.get().strip(), "ID": []}
        self.data["技"].append(move)
        self.index = len(self.data["技"]) - 1
        self.set_ids(move, ids)
        for var in (self.m_ids, self.m_name, self.m_cmd):
            var.set("")
        self.retry()

    def skip(self):
        """登録しない技（投げなど）に印を付けて次へ。スキップ済みの技で押すと印を外す"""
        move = self.data["技"][self.index]
        if move.get("スキップ"):
            del move["スキップ"]
            self.save(f"技登録 {move['技名']}（{move['コマンド']}） のスキップを外しました")
            self.show()
        else:
            move["スキップ"] = True
            self.save(f"技登録 {move['技名']}（{move['コマンド']}） をスキップ")
            self.go_next_todo()

    def find(self):
        """ID欄のIDを使う技（ID欄が空なら技名欄の文字を含む技）を探して表示する。押すたびに次の候補へ"""
        name = self.m_name.get().strip()
        if self.m_ids.get().strip():
            ids = self.typed_ids()
            if ids is None or len(ids) != 1:
                self.app.add_log("探すIDを1つだけID欄に入れてください（例: 0E3A）", "mark")
                return
            key = f"{ids[0]:04X}"
            hit, what = (lambda m: key in m["ID"]), key
        elif name:
            hit, what = (lambda m: name in m["技名"]), f"「{name}」"
        else:
            self.app.add_log("ID欄にID、または技名欄に技名の一部を入れてから「探す」を押してください", "mark")
            return
        # 今の技表の次の技から探し、無ければ他の技表も探す
        stems = list(self.paths)
        start = stems.index(self.file_var.get())
        for n in range(len(stems) + 1):
            stem = stems[(start + n) % len(stems)]
            moves = self.app.waza[self.paths[stem]]["技"]
            first = self.index + 1 if n == 0 else 0
            last = self.index + 1 if n == len(stems) else len(moves)
            found = next((i for i in range(first, last) if hit(moves[i])), None)
            if found is not None:
                if stem != self.file_var.get():
                    self.file_var.set(stem)
                    self.path = self.paths[stem]
                    self.prefix = int(self.data["ID上2桁"], 16) if self.data["ID上2桁"] else None
                self.index = found
                self.retry()
                return
        if self.m_ids.get().strip() and ids[0] in KNOWN_IDS:
            self.app.add_log(f"{what} を使う技はありません（表示: {KNOWN_IDS[ids[0]][0]}。待機か表示名だけのIDです）", "mark")
        else:
            self.app.add_log(f"{what} を使う技は見つかりませんでした", "mark")

    def delete_move(self):
        """「wikiに無い技を足す」で足した技を技表から消す（wikiの技は消さずにスキップを使う）"""
        moves = self.data["技"]
        move = moves[self.index]
        if move.get("分類") != "技表に無い技":
            self.app.add_log("消せるのは「wikiに無い技を足す」で足した技だけです（wikiの技はスキップを使ってください）", "mark")
            return
        if not messagebox.askyesno("技を消す", f"{move['技名']}（{move['コマンド']}）を技表から消しますか？", parent=self.top):
            return
        del moves[self.index]
        self.index = min(self.index, len(moves) - 1)
        self.save(f"技登録 {move['技名']}（{move['コマンド']}） を技表から消しました")
        self.retry()

    @property
    def idle_ids(self):
        return {int(i, 16) for i in self.data.get("待機ID", [])}

    def add_idle(self, move):
        self.data.setdefault("待機ID", []).append(f"{move:04X}")
        self.save(f"技登録 {move:04X} を {self.data['キャラ']} の待機の動作にしました")
        self.chain = [i for i in self.chain if i != move]
        self.show()

    def add_typed_idle(self):
        ids = self.typed_ids()
        if ids is None or len(ids) != 1:
            self.app.add_log("待機にするIDを1つだけID欄に入れてください（例: 0E01）", "mark")
            return
        if ids[0] not in self.idle_ids:
            self.add_idle(ids[0])
        self.m_ids.set("")

    def clear_idle(self):
        if self.data.get("待機ID"):
            del self.data["待機ID"]
            self.save(f"技登録 {self.data['キャラ']} の待機IDを消しました")
            self.show()

    def save(self, log_text):
        save_waza(self.path, self.data)
        build_known_ids(self.app.waza)
        self.app.add_log(log_text, "mark")
        self.app.redraw_log()

    def matches(self, move):
        if self.prefix is None:
            return move >> 8 != 0  # ID上2桁が未確認なら、共通以外は全部拾う
        if self.prefix:
            return move >> 8 == self.prefix
        return move >> 8 == 0 and move not in IDLE_MOVES  # 共通の表は動作も含めて拾う

    def on_tick(self, now, move):
        if move != self.cur_id:
            self.cur_id, self.cur_since = move, now
        elif (now - self.cur_since >= IDLE_HOLD_SEC and self.matches(move)
              and not self.idle_ids and self.chain == [move]):
            # 技を出す前から長く続くIDは待機の動作。技表に覚えさせて、並びから外す
            # （技の後に続く構えを待機と間違えないよう、待機IDがまだ無いキャラの、並びの先頭の時だけ）
            self.add_idle(move)
        if not self.matches(move) or move in self.idle_ids:
            return
        gap = now - self.last_seen > ATTEMPT_GAP_SEC
        self.last_seen = now
        if gap or not self.chain:
            self.chain = [move]
        elif self.chain[-1] != move:
            self.chain.append(move)
        else:
            return
        self.show()

    def show(self):
        moves = self.data["技"]
        done = sum(bool(m["ID"]) for m in moves)
        skipped = sum(bool(m.get("スキップ")) for m in moves)
        self.progress.config(text=f"{self.index + 1}/{len(moves)}  （ID登録済み {done}  スキップ {skipped}）")
        move = moves[self.index]
        memo = f"  ※{move['メモ']}" if move.get("メモ") else ""
        self.move_label.config(text=f"[{move.get('分類', '')}] {move['技名']}{memo}\n{move['コマンド']}")
        # 登録済みなら「済」と同じ水色、まだなら灰色で、一目で分かるようにする
        self.saved_label.config(text="登録済みのID: " + ("→".join(move["ID"]) or "なし")
                                + ("  （スキップ中）" if move.get("スキップ") else ""),
                                fg="#7fd4ff" if move["ID"] else "gray60")
        self.idle_label.config(text="待機とみなすID: " + (" ".join(self.data.get("待機ID", [])) or "まだなし"))
        own = {int(i, 16) for i in move["ID"]}
        if self.chain:
            lines = []
            for i in self.chain:
                if i in own:
                    lines.append((f"済 {i:04X} {KNOWN_IDS[i][label_mode]}（この技）", "this"))
                elif i in KNOWN_IDS:
                    lines.append((f"済 {i:04X} {KNOWN_IDS[i][label_mode]}", "other"))
                else:
                    lines.append((f"新 {i:04X} （未登録）", "new"))
        else:
            lines = [("この技を出してください（出たIDがここに並びます）", "info")]
        self.chain_text.config(state="normal", height=len(lines))
        self.chain_text.delete("1.0", "end")
        for n, (text, tag) in enumerate(lines):
            self.chain_text.insert("end", ("\n" if n else "") + text, tag)
        self.chain_text.config(state="disabled")


class App:
    def __init__(self):
        self.gdb = GdbClient()
        self.start = time.monotonic()
        self.prev_slots = None
        self.prev_hp = None
        self.prev_move = self.prev_last_hit = self.prev_enemy_move = None
        self.prev_playsec = None
        self.prev_name_id = self.prev_area = None
        self.names = {}  # 名前番号 → 名前（ゲームの固定データなので読んだら覚えておく）
        self.flash_until = [0.0] * SLOT_COUNT
        self.moves = deque()  # (時刻, 技ID)
        self.log_entries = deque(maxlen=LOG_LINES)  # (時刻表示, 本文, タグ)。表示切り替えで書き直す
        self.playtime = ""
        self.pending_write = None
        self.register_window = None
        self.waza = {}

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.txt"
        self.log_file = self.log_path.open("a", encoding="utf-8")

        self.root = tk.Tk()
        self.root.title("Tobal2 JKガード監視")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        # Mac・Windowsどちらにもある等幅フォントを使う
        self.fonts = fonts = {}
        for size in (13, 15, 16):
            fonts[size] = tkfont.nametofont("TkFixedFont").copy()
            fonts[size].configure(size=size)

        top = tk.Frame(self.root, bg="black")
        top.pack(fill="x")
        tk.Button(top, text="技登録", command=self.open_register).pack(side="right", anchor="ne", padx=10, pady=6)
        self.status = tk.Label(
            top, text="", font=fonts[15], fg="white", bg="black",
            justify="left", anchor="w", padx=10, pady=6,
        )
        self.status.pack(side="left", fill="x")

        self.table = tk.Text(
            self.root, font=fonts[16], fg="white", bg="black", width=40,
            height=SLOT_COUNT + 3, padx=10, borderwidth=0, highlightthickness=0,
            state="disabled", cursor="arrow",
        )
        self.table.pack(fill="x")
        self.table.tag_configure("head", foreground="gray60")
        self.table.tag_configure("current", foreground="#7fd4ff")
        self.table.tag_configure("flash", background="#b03030")
        self.table.tag_configure("empty", foreground="gray35")

        controls = tk.Frame(self.root, bg="black", padx=10, pady=4)
        controls.pack(fill="x")
        tk.Button(controls, text="ガード！", command=lambda: self.mark("★ガード")).pack(side="left")
        tk.Button(controls, text="ガード無し", command=lambda: self.mark("☆ガード無し")).pack(side="left")
        tk.Button(controls, text="区切り", command=lambda: self.mark("────")).pack(side="left")
        self.mode_button = tk.Button(controls, text="表示: 技名", command=self.toggle_label)
        self.mode_button.pack(side="left", padx=(8, 0))

        writer = tk.Frame(self.root, bg="black", padx=10, pady=4)
        writer.pack(fill="x")
        self.w_slot = tk.StringVar(value="9")
        self.w_id = tk.StringVar(value="0199")
        self.w_count = tk.StringVar(value="8")
        for label, var, width in (("枠", self.w_slot, 3), ("ID(16進)", self.w_id, 6), ("回数", self.w_count, 5)):
            tk.Label(writer, text=label, fg="white", bg="black").pack(side="left")
            tk.Entry(writer, textvariable=var, width=width).pack(side="left", padx=(2, 8))
        tk.Button(writer, text="書き込み", command=self.request_write).pack(side="left")

        self.log = tk.Text(
            self.root, font=fonts[13], fg="white", bg="gray10", width=40,
            height=14, padx=10, pady=6, borderwidth=0, highlightthickness=0,
            state="disabled",
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("mark", foreground="#ffd84d")
        self.log.tag_configure("info", foreground="gray60")
        self.log.tag_configure("move", foreground="#9fe89f")
        self.log.tag_configure("enemy", foreground="#ff9f7f")

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.add_log(f"ログ保存先: {LOG_DIR.name}/{self.log_path.name}", "info")  # フルパスだとユーザー名が映るので出さない
        self.reload_waza()
        self.root.after(0, self.tick)

    def quit(self):
        self.gdb.close()
        self.log_file.close()
        self.root.destroy()

    def add_log(self, text, tag=""):
        """text は文字列か、技名/コマンドの表示に合わせて本文を作る関数"""
        stamp = f"{time.monotonic() - self.start:8.2f}s {self.playtime}"
        line = f"{stamp} {text() if callable(text) else text}"
        self.log_file.write(line + "\n")
        self.log_file.flush()
        self.log_entries.append((stamp, text, tag))
        self.log.config(state="normal")
        self.log.insert("end", line + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > LOG_LINES:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def redraw_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        for stamp, text, tag in self.log_entries:
            self.log.insert("end", f"{stamp} {text() if callable(text) else text}\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def toggle_label(self):
        global label_mode
        label_mode = 1 - label_mode
        self.mode_button.config(text=f"表示: {LABEL_MODES[label_mode]}")
        self.redraw_log()

    def reload_waza(self):
        self.waza, errors = load_waza()
        build_known_ids(self.waza)
        for error in errors:
            self.add_log(error, "mark")
        summary = " / ".join(f"{d['キャラ']} {sum(bool(m['ID']) for m in d['技'])}/{len(d['技'])}"
                             for d in self.waza.values())
        self.add_log(f"技表（IDあり/全部）: {summary or 'waza フォルダが見つかりません（技名は出ません）'}", "info")

    def open_register(self):
        if self.register_window:
            self.register_window.top.lift()
        elif self.waza:
            self.register_window = Register(self)
        else:
            self.add_log("waza フォルダに技表（.json）がありません", "mark")

    def mark(self, text):
        self.add_log(f"{text}  {self.slots_summary(self.prev_slots)}", "mark")

    def request_write(self):
        try:
            slot = int(self.w_slot.get())
            slot_id = int(self.w_id.get(), 16)
            count = int(self.w_count.get())
            if not (0 <= slot < SLOT_COUNT and 0 <= slot_id <= 0xFFFF and 0 <= count <= 0xFFFF):
                raise ValueError
        except ValueError:
            self.add_log("書き込み値が不正です（枠0〜9、IDは16進、回数は10進）", "info")
            return
        # 実際の書き込みは接続中の tick で行う
        self.pending_write = (slot, slot_id, count)

    def slots_summary(self, slots):
        if not slots:
            return ""
        return " ".join(f"{i}:{s:04X}:{c}" for i, (s, c) in enumerate(slots) if s or c)

    def recent_moves(self, now):
        return [m for t, m in self.moves if now - t <= MOVE_HISTORY_SEC]

    @staticmethod
    def move_flow(moves):
        return "→".join(f"{m:04X} {id_label(m)}".rstrip() for m in moves) or "-"

    def enemy_name(self, name_id):
        if name_id not in self.names:
            ptr = int.from_bytes(self.gdb.read(NAME_TABLE_ADDR + name_id * 4, 4), "little")
            if not RAM <= ptr < RAM + 0x200000:
                return "???"  # 戦闘外などで番号が名前表の範囲外
            raw = self.gdb.read(ptr, NAME_MAX).split(b"\0")[0]
            self.names[name_id] = raw.decode("latin1")
        return self.names[name_id]

    @staticmethod
    def area_label(area):
        return f"{area:02X}" + (f" {AREA_NAMES[area]}" if area in AREA_NAMES else "")

    def read_state(self):
        read = self.gdb.read
        table = read(TABLE_TOP, TABLE_SIZE)
        slots = [
            (u16(table, SLOT_OFFSET + i * 4), u16(table, SLOT_OFFSET + i * 4 + 2))
            for i in range(SLOT_COUNT)
        ]
        area_floor = read(AREA_ADDR, FLOOR_ADDR - AREA_ADDR + 1)
        playtime = read(PLAYTIME_ADDR, 3)
        return {
            "timer": u16(table, 0),
            "slots": slots,
            "group": read(GROUP_ADDR, 1)[0],
            "hp": u16(read(ENEMY_HP_ADDR, 2), 0),
            "move": u16(read(MOVE_ADDR, 2), 0),
            "last_hit": u16(read(LAST_HIT_ADDR, 2), 0),
            "enemy_move": u16(read(ENEMY_MOVE_ADDR, 2), 0),
            "name_id": read(ENEMY_NAME_ID_ADDR, 1)[0],
            "area": area_floor[0],
            "floor": area_floor[-1],
            "playtime": f"{playtime[2]}:{playtime[1]:02}:{playtime[0]:02}",
            "playsec": playtime[2] * 3600 + playtime[1] * 60 + playtime[0],
        }

    def detect_load(self, state):
        """ステートロードらしき変化（プレイ時間が飛ぶ／表の複数枠が同時に変わる）を検出する"""
        prev_sec, self.prev_playsec = self.prev_playsec, state["playsec"]
        if self.prev_slots is None or prev_sec is None:
            return False
        jumped = not (0 <= state["playsec"] - prev_sec <= 2)
        changed = sum(a != b for a, b in zip(self.prev_slots, state["slots"]))
        if not (jumped or changed >= LOAD_SLOT_CHANGES):
            return False
        self.add_log(
            f"◆ステートロード？ {self.playtime}→{state['playtime']}  "
            f"{self.slots_summary(state['slots'])}", "mark")
        return True

    def apply_writes(self):
        if self.pending_write:
            slot, slot_id, count = self.pending_write
            self.pending_write = None
            addr = TABLE_TOP + SLOT_OFFSET + slot * 4
            self.gdb.write(addr, slot_id.to_bytes(2, "little") + count.to_bytes(2, "little"))
            self.add_log(f"書き込み 枠{slot} = {slot_id:04X}:{count}", "mark")
            self.prev_slots = None  # 書き込みを増加として拾わない

    def detect_changes(self, state, now):
        hp, slots = state["hp"], state["slots"]
        if state["move"] != self.prev_move and state["move"] not in IDLE_MOVES:
            self.moves.append((now, state["move"]))
        while self.moves and now - self.moves[0][0] > MOVE_HISTORY_SEC:
            self.moves.popleft()

        if state["move"] != self.prev_move and self.prev_move is not None:
            move = state["move"]
            self.add_log(lambda: f"技 {move:04X} {id_label(move)}", "move")
        if state["last_hit"] != self.prev_last_hit and self.prev_last_hit is not None:
            last_hit = state["last_hit"]
            self.add_log(lambda: f"  最後に当てた {last_hit:04X} {id_label(last_hit)}", "info")
        if state["enemy_move"] != self.prev_enemy_move and self.prev_enemy_move is not None:
            self.add_log(f"    敵の技 {state['enemy_move']:04X}", "enemy")
            if state["enemy_move"] in ENEMY_GUARD_MOVES:
                move, summary = state["move"], self.slots_summary(state["slots"])
                kind = ENEMY_GUARD_MOVES[state["enemy_move"]]
                name = self.enemy_name(state["name_id"])
                self.add_log(lambda: f"◎敵が{kind}（自分の技 {move:04X} {id_label(move)}）  {summary}  敵 {name}", "mark")
        self.prev_move, self.prev_last_hit = state["move"], state["last_hit"]
        self.prev_enemy_move = state["enemy_move"]

        if state["area"] != self.prev_area:
            self.add_log(f"エリア {self.area_label(state['area'])}  FLOOR {state['floor']}", "mark")
        if state["name_id"] != self.prev_name_id:
            self.add_log(f"敵の名前 {self.enemy_name(state['name_id'])}（番号 {state['name_id']}）", "info")
        self.prev_area, self.prev_name_id = state["area"], state["name_id"]

        if self.prev_hp is not None and hp > self.prev_hp:
            self.add_log(f"敵HP {self.prev_hp}→{hp}（敵交代？） 敵 {self.enemy_name(state['name_id'])}"
                         f"  エリア {self.area_label(state['area'])} FLOOR {state['floor']}", "info")

        if self.prev_slots is not None:
            for i, ((old_id, old_count), (new_id, new_count)) in enumerate(zip(self.prev_slots, slots)):
                if (old_id, old_count) == (new_id, new_count):
                    continue
                kind = "メモ" if i < CURRENT_SLOTS else "ノート"
                if new_id != old_id:
                    detail = f"{old_id:04X}:{old_count} → {new_id:04X}:{new_count}"
                else:
                    detail = f"{new_id:04X} {old_count}→{new_count}"
                moves = self.recent_moves(now)
                self.add_log(lambda i=i, kind=kind, detail=detail, new_id=new_id, moves=moves:
                             f"枠{i}({kind}) {detail} {id_label(new_id)}  直前の技[{self.move_flow(moves)}]")
                if new_count > old_count or new_id != old_id:
                    self.flash_until[i] = now + FLASH_SEC

        self.prev_hp, self.prev_slots = hp, slots

    def render(self, state, now):
        self.status.config(text=(
            f"今の技 {state['move']:04X} {id_label(state['move'])}   敵の技 {state['enemy_move']:04X}\n"
            f"群 {state['group']:>3}   敵HP {state['hp']:>4}   timer {state['timer']:>3}\n"
            f"敵 {self.enemy_name(state['name_id'])}\n"
            f"エリア {self.area_label(state['area'])}  FLOOR {state['floor']}   {state['playtime']}"
        ))
        self.table.config(state="normal")
        self.table.delete("1.0", "end")
        self.table.insert("end", f"枠  ID    回数  {LABEL_MODES[label_mode]}\n", "head")
        self.table.insert("end", "── その敵だけのメモ ──\n", "head")
        for i, (slot_id, count) in enumerate(state["slots"]):
            if i == CURRENT_SLOTS:
                self.table.insert("end", "── 記憶ノート ──\n", "head")
            tags = ["current" if i < CURRENT_SLOTS else ""]
            if not slot_id and not count:
                tags = ["empty"]
            if now < self.flash_until[i]:
                tags.append("flash")
            row = f"{i:>2}  {slot_id:04X}  {count:>4}  {id_label(slot_id) if slot_id else ''}"
            self.table.insert("end", row + "\n", tuple(tags))
        self.table.config(state="disabled")

    def tick(self):
        try:
            if self.gdb.sock is None:
                self.gdb.connect()
                self.add_log("DuckStationに接続しました", "info")
            self.apply_writes()
            state = self.read_state()
        except OSError as e:
            if self.gdb.sock is not None:
                self.add_log(f"接続が切れました: {e}", "info")
            self.gdb.close()
            self.prev_slots = self.prev_hp = None
            self.prev_move = self.prev_last_hit = self.prev_enemy_move = None
            self.prev_name_id = self.prev_area = None
            self.status.config(text="DuckStation 未接続\n(GDBサーバー待ち)")
            self.root.after(RETRY_MS, self.tick)
            return

        now = time.monotonic()
        if self.detect_load(state):
            # ロードによる入れ替わりは技の増加として拾わない
            self.prev_slots = self.prev_hp = None
        self.playtime = state["playtime"]
        self.detect_changes(state, now)
        if self.register_window:
            self.register_window.on_tick(now, state["move"])
        self.render(state, now)
        self.root.after(POLL_MS, self.tick)


if __name__ == "__main__":
    App().root.mainloop()
