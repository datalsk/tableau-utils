#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tableau Parts Supply Sync - 설정 GUI 버전

비개발자도 쓸 수 있도록, 연결/경로 설정을 창에서 입력하고
[실행] 버튼으로 동기화를 수행한다. 설정은 실행파일 옆 config.json 에 저장된다.

빌드: build.bat 참고 (Windows에서 1회 빌드)
"""
import os
import sys
import json
import threading
import traceback
import shutil
import tempfile
import zipfile

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# 무거운 라이브러리는 GUI 기동을 막지 않도록 가드해서 임포트
IMPORT_ERROR = ""
try:
    import pandas as pd
    import tableauserverclient as TSC
    from tableauhyperapi import HyperProcess, Telemetry, Connection
    import pantab
except Exception as e:  # 라이브러리 미설치 시에도 창은 뜨게
    IMPORT_ERROR = repr(e)




# ===== 설정 파일 경로 (스크립트/실행파일 양쪽에서 동작) =====
def app_dir():
    if getattr(sys, "frozen", False):       # PyInstaller로 묶인 exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    "server_url": "https://your-tableau-server.example.com/",
    "site_name": "",
    "token_name": "your-token-name",
    "token_value": "",
    "datasource_name": "your-datasource-name",
    "project_path": [
        "Top-level Project",
        "Sub Project",
    ],
    "rawdata_folder": "",
    "output_folder": "",
    "sheet_name": "Sheet1",
    "anchor": "anchor-column-name",
    "text_cols": [],
    "keep_cols": [],
    "sum_cols": [],
    "mean_cols": [],
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update({k: saved[k] for k in saved if k in DEFAULT_CONFIG})
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ================================================================
# 동기화 로직 (기존 스크립트와 동일, print -> log 로만 교체)
# ================================================================
def hyper_to_dataframe(hyper_path):
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, hyper_path) as conn:
            frames = []
            for schema in conn.catalog.get_schema_names():
                for table in conn.catalog.get_table_names(schema):
                    table_def = conn.catalog.get_table_definition(table)
                    cols = [c.name.unescaped for c in table_def.columns]
                    rows = conn.execute_list_query(f"SELECT * FROM {table}")
                    frames.append(pd.DataFrame(rows, columns=cols))
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def get_tableau_df(cfg, log):
    tmp_dir = tempfile.mkdtemp()
    try:
        auth = TSC.PersonalAccessTokenAuth(
            cfg["token_name"], cfg["token_value"], site_id=cfg["site_name"]
        )
        server = TSC.Server(cfg["server_url"], use_server_version=True)
        with server.auth.sign_in(auth):
            all_ds = list(TSC.Pager(server.datasources))
            ds = next((d for d in all_ds if d.name == cfg["datasource_name"]), None)
            if ds is None:
                raise ValueError(
                    f"데이터소스 '{cfg['datasource_name']}' 없음. 사용 가능: {[d.name for d in all_ds]}"
                )
            log(f"Tableau 데이터소스 발견: {ds.name} (ID: {ds.id})")
            server.datasources.download(ds.id, tmp_dir, include_extract=True)

        downloaded = os.listdir(tmp_dir)
        tdsx = next((os.path.join(tmp_dir, f) for f in downloaded if f.endswith(".tdsx")), None)
        if tdsx:
            with zipfile.ZipFile(tdsx, "r") as z:
                names = [n for n in z.namelist() if n.endswith(".hyper")]
                if not names:
                    raise FileNotFoundError(f".tdsx 내부에 .hyper 없음: {z.namelist()}")
                z.extract(names[0], tmp_dir)
                hyper_file = os.path.join(tmp_dir, names[0])
        else:
            hyper_file = next((os.path.join(tmp_dir, f) for f in downloaded if f.endswith(".hyper")), None)

        if hyper_file is None:
            raise FileNotFoundError(f"Hyper 파일 못 찾음: {downloaded}")

        return hyper_to_dataframe(hyper_file)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def read_with_auto_header(path, anchor, sheet_name, text_cols):
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    mask = raw.astype(str).apply(lambda row: row.str.contains(anchor, na=False)).any(axis=1)
    matches = mask[mask].index
    if len(matches) == 0:
        raise ValueError(f"'{anchor}' 가 들어있는 행을 못 찾음: {path}")
    return pd.read_excel(
        path,
        sheet_name=sheet_name,
        header=matches[0],
        dtype={c: str for c in text_cols}
    )


def aggregate_df(d, keep_cols, text_cols, sum_cols, mean_cols):
    d.columns = d.columns.astype(str).str.strip()
    d = d[[c for c in keep_cols if c in d.columns]].copy()

    for c in text_cols:
        if c in d.columns:
            d[c] = d[c].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()

    use_sum  = [c for c in sum_cols  if c in d.columns]
    use_mean = [c for c in mean_cols if c in d.columns]
    for c in use_sum + use_mean:
        d[c] = pd.to_numeric(d[c].astype(str).str.replace(',', '', regex=False), errors='coerce')

    measures = set(use_sum + use_mean)
    dim_cols = [c for c in d.columns if c not in measures]

    agg_map = {c: 'sum' for c in use_sum}
    agg_map.update({c: 'mean' for c in use_mean})

    g = d.groupby(dim_cols, dropna=False, as_index=False).agg(agg_map)
    return g[[c for c in keep_cols if c in g.columns]]


def prepare_for_hyper(df, text_cols):
    df = df.copy()

    for c in text_cols:
        if c in df.columns:
            df[c] = df[c].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()

    return df


def find_project_by_path(server, project_path):
    all_projects = list(TSC.Pager(server.projects))

    current_parent_id = None
    current_project = None

    for name in project_path:
        candidates = [
            p for p in all_projects
            if p.name == name and getattr(p, "parent_id", None) == current_parent_id
        ]

        if not candidates:
            available = [(p.name, getattr(p, "parent_id", None)) for p in all_projects]
            raise ValueError(
                f"프로젝트 경로를 찾을 수 없습니다: {project_path}\n"
                f"현재 찾으려는 이름: {name}, parent_id: {current_parent_id}\n"
                f"사용 가능한 프로젝트 목록: {available}"
            )

        current_project = candidates[0]
        current_parent_id = current_project.id

    return current_project


def publish_hyper_overwrite(cfg, hyper_path, log):
    auth = TSC.PersonalAccessTokenAuth(
        cfg["token_name"], cfg["token_value"], site_id=cfg["site_name"]
    )
    server = TSC.Server(cfg["server_url"], use_server_version=True)

    with server.auth.sign_in(auth):
        project = find_project_by_path(server, cfg["project_path"])

        datasource_item = TSC.DatasourceItem(
            project_id=project.id,
            name=cfg["datasource_name"]
        )

        published = server.datasources.publish(
            datasource_item,
            hyper_path,
            mode=TSC.Server.PublishMode.Overwrite
        )

        log(f"게시 대상 프로젝트: {project.name} (ID: {project.id})")
        log(f"게시(덮어쓰기) 완료: {published.name} (ID: {published.id})")


def run_sync(cfg, log):
    """GUI [실행] 버튼이 백그라운드 스레드에서 호출."""
    if IMPORT_ERROR:
        log("[오류] 필수 라이브러리 로드 실패: " + IMPORT_ERROR)
        log("       실행파일이 정상적으로 빌드되지 않았을 수 있습니다.")
        return

    # --- 입력값 점검 ---
    if not cfg["token_value"].strip():
        log("[오류] 토큰 값(Token value)이 비어 있습니다.")
        return
    if not os.path.isdir(cfg["rawdata_folder"]):
        log(f"[오류] Rawdata 폴더가 없습니다: {cfg['rawdata_folder']}")
        return
    os.makedirs(cfg["output_folder"], exist_ok=True)

    excel_output_path = os.path.join(cfg["output_folder"], "prepdata.xlsx")
    hyper_output_path = os.path.join(cfg["output_folder"], "parts_supply.hyper")

    text_cols = cfg.get("text_cols", [])
    keep_cols = cfg.get("keep_cols", [])
    sum_cols  = cfg.get("sum_cols", [])
    mean_cols = cfg.get("mean_cols", [])

    agg_list = []

    # 1) Tableau 서버 데이터 - 원본 그대로 사용
    log("=== Tableau 서버 데이터소스 조회 ===")
    df_tab = get_tableau_df(cfg, log)
    log(f"Tableau 원본: {df_tab.shape}")
    if not df_tab.empty:
        agg_list.append(df_tab)
        log(f"  -> Tableau 데이터: 원본 그대로 사용: {df_tab.shape}")

    # 2) 로컬 엑셀 데이터 (parts_raw*) - 집계
    folder = cfg["rawdata_folder"]
    files = sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().startswith('parts_raw')
        and f.lower().endswith(('.xls', '.xlsx'))
        and not f.startswith('~$')
    )
    log(f"로컬 엑셀 파일 {len(files)}개")
    for f in files:
        d = read_with_auto_header(f, anchor=cfg["anchor"], sheet_name=cfg["sheet_name"], text_cols=text_cols)
        agg_list.append(aggregate_df(d, keep_cols=keep_cols, text_cols=text_cols, sum_cols=sum_cols, mean_cols=mean_cols))
        log(f"  -> {os.path.basename(f)}: 집계 적용 -> {agg_list[-1].shape}")

    if not agg_list:
        log("[오류] 합칠 데이터가 없습니다. Tableau/로컬 엑셀을 확인하세요.")
        return

    # 3) 유니온
    df = pd.concat(agg_list, ignore_index=True)
    log(f"최종 shape: {df.shape}")

    # 4) 엑셀 저장
    df.to_excel(excel_output_path, sheet_name='집계결과', index=False, engine='openpyxl')
    log(f"엑셀 저장 완료: {excel_output_path}")

    # 5) Hyper 저장
    df = prepare_for_hyper(df, text_cols=text_cols)
    pantab.frame_to_hyper(df, hyper_output_path, table="Extract")
    log(f"Hyper 저장 완료: {hyper_output_path}")

    # 6) Tableau 서버 업로드
    publish_hyper_overwrite(cfg, hyper_output_path, log)
    log("=== 전체 완료 ===")


# ================================================================
# GUI
# ================================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title("Tableau Parts Supply Sync")
        root.geometry("680x720")

        cfg = load_config()
        self.vars = {}

        pad = {"padx": 8, "pady": 3}
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        r = 0

        def add_entry(label, key, show=None, width=52):
            nonlocal r
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", **pad)
            v = tk.StringVar(value=cfg[key])
            e = ttk.Entry(frm, textvariable=v, width=width, show=show)
            e.grid(row=r, column=1, columnspan=2, sticky="we", **pad)
            self.vars[key] = v
            r += 1

        def add_folder(label, key):
            nonlocal r
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", **pad)
            v = tk.StringVar(value=cfg[key])
            ttk.Entry(frm, textvariable=v, width=44).grid(row=r, column=1, sticky="we", **pad)
            ttk.Button(frm, text="찾아보기",
                       command=lambda vv=v: self._browse(vv)).grid(row=r, column=2, **pad)
            self.vars[key] = v
            r += 1

        ttk.Label(frm, text="[ Tableau 연결 ]", font=("", 10, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 2)); r += 1
        add_entry("Server URL", "server_url")
        add_entry("Site name (보통 비움)", "site_name")
        add_entry("Token name", "token_name")
        add_entry("Token value", "token_value", show="*")
        add_entry("Datasource name", "datasource_name")

        ttk.Label(frm, text="[ 게시 대상 프로젝트 경로 ] (한 줄에 한 단계)",
                  font=("", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 2)); r += 1
        self.project_text = tk.Text(frm, height=6, width=52)
        self.project_text.insert("1.0", "\n".join(cfg["project_path"]))
        self.project_text.grid(row=r, column=0, columnspan=3, sticky="we", padx=8, pady=3); r += 1

        ttk.Label(frm, text="[ 경로 ]", font=("", 10, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 2)); r += 1
        add_folder("Rawdata 폴더", "rawdata_folder")
        add_folder("출력 폴더", "output_folder")

        ttk.Label(frm, text="[ 엑셀 읽기 ]", font=("", 10, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 2)); r += 1
        add_entry("시트 이름", "sheet_name")
        add_entry("Anchor 단어", "anchor")

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 4)); r += 1
        ttk.Button(btns, text="설정 저장", command=self.on_save).pack(side="left", padx=4)
        self.run_btn = ttk.Button(btns, text="실행", command=self.on_run)
        self.run_btn.pack(side="left", padx=4)

        ttk.Label(frm, text="[ 진행 로그 ]", font=("", 10, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 2)); r += 1
        self.logbox = ScrolledText(frm, height=12, state="disabled", wrap="word")
        self.logbox.grid(row=r, column=0, columnspan=3, sticky="nsew", padx=8, pady=3); r += 1

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(r - 1, weight=1)

        if IMPORT_ERROR:
            self.log("[경고] 라이브러리 로드 실패 — 빌드 환경을 확인하세요: " + IMPORT_ERROR)

    def _browse(self, var):
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def collect(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        lines = self.project_text.get("1.0", "end").splitlines()
        cfg["project_path"] = [ln.strip() for ln in lines if ln.strip()]
        return cfg

    def on_save(self):
        try:
            save_config(self.collect())
            messagebox.showinfo("저장", f"설정을 저장했습니다.\n{CONFIG_PATH}")
        except Exception as e:
            messagebox.showerror("저장 실패", str(e))

    def log(self, msg):
        self.root.after(0, lambda: self._append(msg))

    def _append(self, msg):
        self.logbox.config(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def on_run(self):
        cfg = self.collect()
        try:
            save_config(cfg)   # 실행 시 자동 저장
        except Exception:
            pass
        self.run_btn.config(state="disabled")

        def worker():
            try:
                run_sync(cfg, self.log)
            except Exception:
                self.log("[예외 발생]\n" + traceback.format_exc())
            finally:
                self.root.after(0, lambda: self.run_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
