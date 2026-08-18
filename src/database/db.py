from pathlib import Path
import sqlite3

# Đường dẫn từ src/database/db.py -> Thư mục gốc dự án
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

# Tự động tạo thư mục 'data' nếu chưa có
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "database.db"


def get_connection():
    """Tạo kết nối SQLite, trả về kết quả dạng dict-like (sqlite3.Row)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Khởi tạo database, các bảng và index nếu chưa tồn tại."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS TBMT (
        id TEXT PRIMARY KEY,
        planId TEXT,
        maTBMT TEXT,
        ngayDangTaiGoc DATETIME,
        thoiGianSuaTBMT DATETIME,
        maKHLCNT TEXT,
        tenDuToanMuaSam TEXT,
        quyTrinhApDung TEXT,
        tenGoiThau TEXT,
        chuDauTu TEXT,
        hinhThucLuaChonNhaThau TEXT,
        phuongThucLuaChonNhaThau TEXT,
        linhVuc TEXT,
        loaiHopDong TEXT,
        thoiGianThucHienGoiThau TEXT,
        thoiDiemDongThau DATETIME,
        thoiDiemMoThau DATETIME,
        hieuLucHoSoDuThau TEXT,
        soTienBaoDamDuThau TEXT,
        duToanMuaSam TEXT,
        giaGoiThau TEXT,
        trangThaiTBMT TEXT,
        nguoiTrungThau TEXT,
        giaTrungThau TEXT
    );

    CREATE TABLE IF NOT EXISTS telegram_subscribers (
        chat_id TEXT PRIMARY KEY,
        username TEXT,
        role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL UNIQUE,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_maTBMT ON TBMT(maTBMT);
    CREATE INDEX IF NOT EXISTS idx_maKHLCNT ON TBMT(maKHLCNT);
    CREATE INDEX IF NOT EXISTS idx_trangThaiTBMT ON TBMT(trangThaiTBMT);
    CREATE INDEX IF NOT EXISTS idx_thoiDiemDongThau ON TBMT(thoiDiemDongThau);
    """

    with get_connection() as conn:
        conn.executescript(schema_sql)
    print("✅ Đã khởi tạo database và các bảng thành công tại:", DB_PATH)



def upsert_subscriber(chat_id: str, username: str, role: str = "user"):
    sql = """
    INSERT INTO telegram_subscribers (chat_id, username, role)
    VALUES (?, ?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET
        username = excluded.username,
        role = excluded.role;
    """
    with get_connection() as conn:
        conn.execute(sql, (str(chat_id), username, role))


def remove_subscriber(chat_id: str) -> bool:
    sql = "DELETE FROM telegram_subscribers WHERE chat_id = ?"
    with get_connection() as conn:
        cursor = conn.execute(sql, (str(chat_id),))
        return cursor.rowcount > 0


def get_subscriber_role(chat_id: str) -> str | None:
    sql = "SELECT role FROM telegram_subscribers WHERE chat_id = ?"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (str(chat_id),))
        row = cursor.fetchone()
        return row["role"] if row else None


def get_all_subscribers() -> list[dict]:
    sql = "SELECT chat_id, username, role FROM telegram_subscribers ORDER BY created_at DESC"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


# --- HÀM CHO KEYWORD ---
def add_keyword(keyword: str) -> bool:
    sql = "INSERT OR IGNORE INTO keywords (keyword) VALUES (?)"
    with get_connection() as conn:
        cursor = conn.execute(sql, (keyword.strip().lower(),))
        return cursor.rowcount > 0


def remove_keyword(keyword: str) -> bool:
    sql = "DELETE FROM keywords WHERE keyword = ?"
    with get_connection() as conn:
        cursor = conn.execute(sql, (keyword.strip().lower(),))
        return cursor.rowcount > 0


def get_all_keywords() -> list[str]:
    sql = "SELECT keyword FROM keywords ORDER BY created_at DESC"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [row["keyword"] for row in cursor.fetchall()]


# --- HÀM CHO TBMT ---

def upsert_tbmt(data: dict):
    sql = """
    INSERT INTO TBMT (
        id, planId, maTBMT, ngayDangTaiGoc, thoiGianSuaTBMT,
        maKHLCNT, tenDuToanMuaSam, quyTrinhApDung, tenGoiThau,
        chuDauTu, hinhThucLuaChonNhaThau, phuongThucLuaChonNhaThau,
        linhVuc, loaiHopDong, thoiGianThucHienGoiThau, thoiDiemDongThau,
        thoiDiemMoThau, hieuLucHoSoDuThau, soTienBaoDamDuThau,
        duToanMuaSam, giaGoiThau, trangThaiTBMT, nguoiTrungThau, giaTrungThau
    )
    VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
    )
    ON CONFLICT(id) DO UPDATE SET
        planId = excluded.planId,
        maTBMT = excluded.maTBMT,
        ngayDangTaiGoc = excluded.ngayDangTaiGoc,
        thoiGianSuaTBMT = excluded.thoiGianSuaTBMT,
        maKHLCNT = excluded.maKHLCNT,
        tenDuToanMuaSam = excluded.tenDuToanMuaSam,
        quyTrinhApDung = excluded.quyTrinhApDung,
        tenGoiThau = excluded.tenGoiThau,
        chuDauTu = excluded.chuDauTu,
        hinhThucLuaChonNhaThau = excluded.hinhThucLuaChonNhaThau,
        phuongThucLuaChonNhaThau = excluded.phuongThucLuaChonNhaThau,
        linhVuc = excluded.linhVuc,
        loaiHopDong = excluded.loaiHopDong,
        thoiGianThucHienGoiThau = excluded.thoiGianThucHienGoiThau,
        thoiDiemDongThau = excluded.thoiDiemDongThau,
        thoiDiemMoThau = excluded.thoiDiemMoThau,
        hieuLucHoSoDuThau = excluded.hieuLucHoSoDuThau,
        soTienBaoDamDuThau = excluded.soTienBaoDamDuThau,
        duToanMuaSam = excluded.duToanMuaSam,
        giaGoiThau = excluded.giaGoiThau,
        trangThaiTBMT = excluded.trangThaiTBMT,
        nguoiTrungThau = excluded.nguoiTrungThau,
        giaTrungThau = excluded.giaTrungThau;
    """
    params = (
        data.get("id"),
        data.get("planId"),
        data.get("maTBMT"),
        data.get("ngayDangTaiGoc"),
        data.get("thoiGianSuaTBMT"),
        data.get("maKHLCNT"),
        data.get("tenDuToanMuaSam"),
        data.get("quyTrinhApDung"),
        data.get("tenGoiThau"),
        data.get("chuDauTu"),
        data.get("hinhThucLuaChonNhaThau"),
        data.get("phuongThucLuaChonNhaThau"),
        data.get("linhVuc"),
        data.get("loaiHopDong"),
        data.get("thoiGianThucHienGoiThau"),
        data.get("thoiDiemDongThau"),
        data.get("thoiDiemMoThau"),
        data.get("hieuLucHoSoDuThau"),
        data.get("soTienBaoDamDuThau"),
        data.get("duToanMuaSam"),
        data.get("giaGoiThau"),
        data.get("trangThaiTBMT"),
        data.get("nguoiTrungThau"),
        data.get("giaTrungThau"),
    )
    with get_connection() as conn:
        conn.execute(sql, params)


def get_latest_thoi_gian_sua_tbmt() -> str | None:
    sql = """
    SELECT thoiGianSuaTBMT
    FROM TBMT
    WHERE thoiGianSuaTBMT IS NOT NULL
    ORDER BY thoiGianSuaTBMT DESC
    LIMIT 1
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        row = cursor.fetchone()
        return row["thoiGianSuaTBMT"] if row else None


def has_tbmt() -> bool:
    sql = "SELECT 1 FROM TBMT LIMIT 1"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchone() is not None


def check_db_health() -> bool:
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 AS ok")
            row = cursor.fetchone()
            return row["ok"] == 1 if row else False
    except Exception:
        return False


def get_all_tbmt() -> list[dict]:
    sql = "SELECT * FROM TBMT ORDER BY thoiGianSuaTBMT DESC"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def get_tbmt_by_time_range(from_time: str | None = None, to_time: str | None = None) -> list[dict]:
    sql = "SELECT * FROM TBMT WHERE 1 = 1"
    params = []

    if from_time:
        sql += " AND thoiGianSuaTBMT > ?"
        params.append(from_time)

    if to_time:
        sql += " AND thoiGianSuaTBMT < ?"
        params.append(to_time)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

def is_tbmt_valid(item_id: str, ma_tbmt: str) -> bool:
    """Kiểm tra xem cặp (id, maTBMT) có khớp và tồn tại trong DB không."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM TBMT WHERE id = ? AND maTBMT = ? LIMIT 1",
            (str(item_id).strip(), str(ma_tbmt).strip()),
        )
        return cursor.fetchone() is not None       

if __name__ == "__main__":
    init_db()