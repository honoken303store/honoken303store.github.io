# トバル2 JKガード監視（DuckStation用・表示専用）
#
# 敵が覚えている「技の回数の表」を小窓に表示し、敵がガードした時にログを残す。
# ゲームのメモリは読むだけで、書き換えはしない。
#
# 準備: DuckStation の 設定 → デバッグ →「GDB サーバーを有効化」をオン（ポート2345）
# 起動: Windows は  py tobal2_jkguard_monitor.py
#       Mac/Linux は python3 tobal2_jkguard_monitor.py
# ログ: このファイルと同じ場所の logs フォルダに保存される
#
# 表（0x12806C〜、1枠 = 技ID 2バイト + 回数 2バイト、10枠）
#   枠0・1 = その敵だけのメモ（敵が替わると空になる）
#   枠2〜9 = 記憶ノート（ハードリセットまで消えない）
# 1体の敵との戦闘で最初に出した2種類の技は枠0・1に、3種類目以降は枠2〜9に数えられる。

import re
import socket
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

HOST = "127.0.0.1"
PORT = 2345
RAM = 0x80000000                     # PS1のメインRAM先頭

TABLE_ADDR = RAM + 0x12806C          # 表の枠0
SLOT_COUNT = 10
MEMO_SLOTS = 2                       # 枠0・1 = その敵だけのメモ
MOVE_ADDR = RAM + 0x123DE8           # 自分の今の技ID（u16）
ENEMY_MOVE_ADDR = RAM + 0x12602C     # 敵の今の技ID（u16）
ENEMY_NAME_ID_ADDR = RAM + 0x126015  # 敵の名前番号（u8）
NAME_TABLE_ADDR = RAM + 0x0D0798     # 名前のポインタ表（u32 × 番号）
AREA_ADDR = RAM + 0x123108           # エリアID
FLOOR_ADDR = RAM + 0x12311B          # 階層
PLAYTIME_ADDR = RAM + 0x0F8BD9       # プレイ時間（秒・分・時）

JK_ID = 0x0199                       # ジャンプキック（全キャラ共通）
ENEMY_GUARD_MOVES = {0x013C: "立ちガード", 0x008D: "しゃがみガード"}
CHARACTERS = {0x08: "エポン", 0x10: "マリー", 0x16: "チャコ"}
LABEL_MODES = ("技名", "コマンド")
AREA_NAMES = {0x21: "プラクティス", 0x1E: "遺跡", 0x26: "神殿", 0x1C: "封印の迷宮", 0x2F: "フィールド"}
# 分かっている技IDの (技名, コマンド)。ボタンでどちらを出すか切り替えられる
# 連携は1段ごとに別IDなので、コマンドはその段までの入力を書く
# 書き方: 0xID(4桁): ("技名", "コマンド"),  を最後の } の前に足す（記号は半角）。詳しくは README.md
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

POLL_MS = 16
RETRY_MS = 1000
FLASH_SEC = 1.5
NAME_MAX = 24
LOG_DIR = Path(__file__).resolve().parent / "logs"

PACKET_RE = re.compile(rb"\$([^#]*)#[0-9a-fA-F]{2}")


def gdb_packet(body):
    return f"${body}#{sum(body.encode()) % 256:02x}".encode()


def u16(data, offset=0):
    return int.from_bytes(data[offset:offset + 2], "little")


label_mode = 0  # 0=技名、1=コマンド（ボタンで切り替え）


def id_label(move_id):
    if move_id in KNOWN_IDS:
        return KNOWN_IDS[move_id][label_mode]
    return CHARACTERS.get(move_id >> 8, "")


class GdbReader:
    """DuckStationのGDBサーバーからメモリを読む（読み取りのみ）"""

    def __init__(self):
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=0.5)
        self.sock.settimeout(0.5)
        self.sock.sendall(gdb_packet("c"))  # 接続すると一時停止するので再開させる

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def read(self, addr, size):
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


class App:
    def __init__(self):
        self.gdb = GdbReader()
        self.names = {}
        self.prev_slots = self.prev_enemy_move = self.prev_area = None
        self.flash_until = [0.0] * SLOT_COUNT
        self.guards = 0

        LOG_DIR.mkdir(exist_ok=True)
        self.log_file = (LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.txt").open("a", encoding="utf-8")

        self.root = tk.Tk()
        self.root.title("Tobal2 JKガード監視")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        mono = tkfont.nametofont("TkFixedFont").copy()
        mono.configure(size=14)
        big = tkfont.nametofont("TkDefaultFont").copy()
        big.configure(size=22, weight="bold")

        self.jk_label = tk.Label(self.root, font=big, fg="white", bg="black", anchor="w", padx=10)
        self.jk_label.pack(fill="x")
        self.status = tk.Label(self.root, font=mono, fg="white", bg="black",
                               justify="left", anchor="w", padx=10)
        self.status.pack(fill="x")
        move_row = tk.Frame(self.root, bg="black")
        move_row.pack(fill="x")
        self.move_label = tk.Label(move_row, font=mono, fg="#9be89b", bg="black",
                                   justify="left", anchor="w", padx=10)
        self.move_label.pack(side="left", fill="x", expand=True)
        self.mode_button = tk.Button(move_row, text=f"表示: {LABEL_MODES[label_mode]}",
                                     command=self.toggle_label)
        self.mode_button.pack(side="right", padx=10)
        self.table = tk.Text(self.root, font=mono, fg="white", bg="black", width=36,
                             height=SLOT_COUNT + 3, padx=10, borderwidth=0,
                             highlightthickness=0, state="disabled", cursor="arrow")
        self.table.pack(fill="x")
        for tag, opts in {"head": {"foreground": "gray60"}, "memo": {"foreground": "#7fd4ff"},
                          "empty": {"foreground": "gray35"}, "jk": {"foreground": "#ffd84d"},
                          "flash": {"background": "#b03030"}}.items():
            self.table.tag_configure(tag, **opts)
        self.log = tk.Text(self.root, font=mono, fg="white", bg="gray10", width=56, height=10,
                           padx=10, borderwidth=0, highlightthickness=0, state="disabled")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("mark", foreground="#ffd84d")

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(0, self.tick)

    def quit(self):
        self.gdb.close()
        self.log_file.close()
        self.root.destroy()

    def toggle_label(self):
        global label_mode
        label_mode = 1 - label_mode
        self.mode_button.config(text=f"表示: {LABEL_MODES[label_mode]}")

    def add_log(self, text, tag=""):
        line = f"{datetime.now():%H:%M:%S} {text}"
        self.log_file.write(line + "\n")
        self.log_file.flush()
        self.log.config(state="normal")
        self.log.insert("end", line + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def enemy_name(self, name_id):
        if name_id not in self.names:
            ptr = int.from_bytes(self.gdb.read(NAME_TABLE_ADDR + name_id * 4, 4), "little")
            if not RAM <= ptr < RAM + 0x200000:
                return "-"
            self.names[name_id] = self.gdb.read(ptr, NAME_MAX).split(b"\0")[0].decode("latin1")
        return self.names[name_id]

    def read_state(self):
        read = self.gdb.read
        table = read(TABLE_ADDR, SLOT_COUNT * 4)
        playtime = read(PLAYTIME_ADDR, 3)
        return {
            "slots": [(u16(table, i * 4), u16(table, i * 4 + 2)) for i in range(SLOT_COUNT)],
            "move": u16(read(MOVE_ADDR, 2)),
            "enemy_move": u16(read(ENEMY_MOVE_ADDR, 2)),
            "name": self.enemy_name(read(ENEMY_NAME_ID_ADDR, 1)[0]),
            "area": read(AREA_ADDR, 1)[0],
            "floor": read(FLOOR_ADDR, 1)[0],
            "playtime": f"{playtime[2]}:{playtime[1]:02}:{playtime[0]:02}",
        }

    @staticmethod
    def note_jk(slots):
        """記憶ノート（枠2〜9）に書かれたJKの回数"""
        return sum(count for slot_id, count in slots[MEMO_SLOTS:] if slot_id == JK_ID)

    def detect_changes(self, state, now):
        if state["area"] != self.prev_area:
            area = AREA_NAMES.get(state["area"], f"{state['area']:02X}")
            self.add_log(f"エリア {area}  FLOOR {state['floor']}")
        if self.prev_enemy_move is not None and state["enemy_move"] != self.prev_enemy_move \
                and state["enemy_move"] in ENEMY_GUARD_MOVES:
            self.guards += 1
            move = state["move"]
            self.add_log(f"◎{state['name']}が{ENEMY_GUARD_MOVES[state['enemy_move']]}"
                         f"（自分の技 {move:04X} {id_label(move)}）  ノートのJK {self.note_jk(state['slots'])}",
                         "mark")
        if self.prev_slots is not None:
            for i, (old, new) in enumerate(zip(self.prev_slots, state["slots"])):
                if new != old:
                    self.flash_until[i] = now + FLASH_SEC
            old_jk, new_jk = self.note_jk(self.prev_slots), self.note_jk(state["slots"])
            if new_jk != old_jk:
                self.add_log(f"ノートのJK {old_jk}→{new_jk}  敵 {state['name']}")
        self.prev_area, self.prev_enemy_move = state["area"], state["enemy_move"]
        self.prev_slots = state["slots"]

    def render(self, state, now):
        self.jk_label.config(text=f"ノートのJK {self.note_jk(state['slots'])} 回   ガード {self.guards} 回")
        area = AREA_NAMES.get(state["area"], f"{state['area']:02X}")
        self.status.config(text=f"敵 {state['name']}\n{area}  FLOOR {state['floor']}   {state['playtime']}")
        move = state["move"]
        self.move_label.config(text=f"今の技 {move:04X} {id_label(move)}")
        self.table.config(state="normal")
        self.table.delete("1.0", "end")
        self.table.insert("end", "枠  ID    回数  技\n── その敵だけのメモ ──\n", "head")
        for i, (slot_id, count) in enumerate(state["slots"]):
            if i == MEMO_SLOTS:
                self.table.insert("end", "── 記憶ノート ──\n", "head")
            if not slot_id and not count:
                tags = ["empty"]
            elif slot_id == JK_ID:
                tags = ["jk"]
            else:
                tags = ["memo" if i < MEMO_SLOTS else ""]
            if now < self.flash_until[i]:
                tags.append("flash")
            row = f"{i:>2}  {slot_id:04X}  {count:>4}  {id_label(slot_id) if slot_id else ''}"
            self.table.insert("end", row + "\n", tuple(tags))
        self.table.config(state="disabled")

    def tick(self):
        try:
            if self.gdb.sock is None:
                self.gdb.connect()
                self.add_log("DuckStationに接続しました")
            state = self.read_state()
        except OSError:
            self.gdb.close()
            self.prev_slots = self.prev_enemy_move = self.prev_area = None
            self.jk_label.config(text="DuckStation 未接続")
            self.status.config(text="GDBサーバー（ポート2345）を待っています")
            self.move_label.config(text="")
            self.root.after(RETRY_MS, self.tick)
            return
        now = time.monotonic()
        self.detect_changes(state, now)
        self.render(state, now)
        self.root.after(POLL_MS, self.tick)


if __name__ == "__main__":
    App().root.mainloop()
