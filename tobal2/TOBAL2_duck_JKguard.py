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

import re
import socket
import time
import tkinter as tk
import tkinter.font as tkfont
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

# 判明しているIDの (技名, コマンド)（分かったら追記していく）
# 連携は1段ごとに別IDなので、コマンドはその段までの入力を書く
KNOWN_IDS = {
    0x0003: ("立ち", "立ち"), 0x0008: ("前歩き", "→"), 0x0009: ("横移動", "↑/↓"),
    0x0037: ("ガード", "Ｇ"), 0x01D4: ("走り", "走り"),
    0x00D1: ("ジャンプ", "ジャンプ"), 0x00AB: ("ジャンプ昇り蹴り", "ジャンプ昇り攻撃"),
    0x019A: ("JK(出だし)", "ジャンプ攻撃"), 0x0199: ("JK", "ジャンプ攻撃"),
    0x00F3: ("大JK", "走りジャンプ長押し>攻撃"),
    0x02C0: ("掴み下段1", "掴み下1"), 0x02C1: ("掴み下段2以降", "掴み下2以降"),
    # チャコ（2026-10-07 ログで確認）
    0x1617: ("冲捶", "上"), 0x164A: ("勢冲捶", "上長押し"), 0x1618: ("連捶2段目", "上､上"),
    0x1653: ("連捶掌3段目", "上､上､上"), 0x164B: ("天中掌(中)", "(上､上､上 or →上)､中"),
    0x1634: ("攪打頂肘3段目", "上､上､中"), 0x1638: ("連捶肘挫掌4段目", "上､上､中､中"),
    0x1639: ("連捶掃双脚3段目", "上､上､下"), 0x1673: ("裏冲天脚(中)", "(上､上､下 or Ｇ下)､中"),
    0x1684: ("＊天掌", "→上"), 0x1620: ("螺旋脚", "←↑↓上"), 0x16A0: ("空旋脚", "→→上"),
    0x16A2: ("竜尾脚", "↑↑上/↓↓上"), 0x1615: ("螺旋激脚", "Ｇ上"), 0x165E: ("横避双撞掌", "しゃがみ上"),
    0x1622: ("端脚", "中"), 0x1623: ("連端脚2段目", "中､中"), 0x1624: ("連端天脚2段目", "中､上"),
    0x1647: ("連端旋脚2段目", "中､上(旋)"), 0x1631: ("伏勢虎", "中､Ｇ"), 0x162F: ("渦螺旋掌", "中､→"),
    0x162B: ("螺旋掌", "→中"), 0x161E: ("双単鞭", "←↑↓中"), 0x161F: ("単陽打2段目", "←↑↓中､中"),
    0x165B: ("旋踵刃", "←↑↓中+下 / ←←中､中"), 0x1630: ("落踵双脚", "←←中"),
    0x1682: ("魂心螺旋掌?", "↑中､↑↓中"), 0x16A4: ("魂心螺旋掌2?", "↑中､↑↓中､↓"),
    0x162C: ("双統掌", "→→中"), 0x1667: ("練気弾", "←中←"),
    0x163A: ("冲天脚?", "立ち上がり中"), 0x1648: ("冲天脚(長押し)?", "立ち上がり中長押し"),
    0x1658: ("勢壁靠?", "走り始め中"), 0x161B: ("跳飛双捶?", "走り中"),
    0x1626: ("斧刃脚", "下"), 0x1649: ("斧刃連脚2段目", "下､下"), 0x1657: ("幻斧双脚", "下､Ｇ"),
    0x164C: ("踏歩斧刃脚", "方向+下"), 0x164D: ("連斧旋脚2段目", "方向+下､中"),
    0x1652: ("環斧連旋脚3段目", "方向+下､中､中"), 0x164E: ("反撥虎", "方向+下､Ｇ"),
    0x167E: ("双撞掌", "方向+下､Ｇ､上"), 0x1675: ("猛虎爬山", "方向+下､Ｇ､中"),
    0x1681: ("拗歩転刃脚", "→→下"), 0x1619: ("裏双脚", "Ｇ下"), 0x163B: ("立ち下（技名不明）", "立ち上がりに下"),
    0x1660: ("反動脚(走り上)", "走り上"), 0x1662: ("反動三段脚2段目", "走り上､上"),
    0x1663: ("反動三段脚3段目", "走り上､上､上"), 0x1661: ("反動天地脚2段目", "走り上､下"),
    0x164F: ("滑刃腿", "走り下"), 0x165A: ("裏斧刃脚", "後ろ向きで下"),
}
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

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.txt"
        self.log_file = self.log_path.open("a", encoding="utf-8")

        self.root = tk.Tk()
        self.root.title("Tobal2 JKガード監視")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        # Mac・Windowsどちらにもある等幅フォントを使う
        fonts = {}
        for size in (13, 15, 16):
            fonts[size] = tkfont.nametofont("TkFixedFont").copy()
            fonts[size].configure(size=size)

        self.status = tk.Label(
            self.root, text="", font=fonts[15], fg="white", bg="black",
            justify="left", anchor="w", padx=10, pady=6,
        )
        self.status.pack(fill="x")

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
        self.freeze = tk.BooleanVar(value=False)
        tk.Checkbutton(
            controls, text="メモ（枠0・1）を空に固定", variable=self.freeze,
            fg="white", bg="black", selectcolor="gray20",
        ).pack(side="left", padx=8)

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
            self.root, font=fonts[13], fg="white", bg="gray10", width=60,
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
        if self.freeze.get():
            self.gdb.write(TABLE_TOP + SLOT_OFFSET, bytes(4 * CURRENT_SLOTS))

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
        self.render(state, now)
        self.root.after(POLL_MS, self.tick)


if __name__ == "__main__":
    App().root.mainloop()
