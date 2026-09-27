# BUILD: v6.2.4-STRICT-20260927
import os

# ============================================================
# CRITICAL: Kaggle credentials must be present in os.environ
# BEFORE importing or invoking any Kaggle library/CLI code.
# ============================================================

def _clean_env(name):
    return str(os.environ.get(name, '') or '').strip().strip('"').strip("'")

BOT_TOKEN = _clean_env('CHEMBOT_BOT_TOKEN')
KAGGLE_USERNAME = _clean_env('KAGGLE_USERNAME').lower()
KAGGLE_API_TOKEN = _clean_env('KAGGLE_API_TOKEN') or _clean_env('KAGGLE_API')
KAGGLE_KEY = _clean_env('KAGGLE_KEY')

# Keep the raw Render values intact for now.  The original-bot-compatible
# Kaggle credential files/environment are prepared AFTER dependencies are
# installed but BEFORE KaggleApi is imported.
if KAGGLE_USERNAME:
    os.environ['KAGGLE_USERNAME'] = KAGGLE_USERNAME

import re
import sys
import json
import time
import uuid
import base64
import shutil
import tempfile
import subprocess
import threading
import hashlib
import math
import signal
import codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ============================================================
# Chemistry Telegram/Kaggle Bot v6.2.4
# ============================================================


def ensure_dependencies():
    packages = {
        'telebot': 'pyTelegramBotAPI>=4.16',
        'matplotlib': 'matplotlib>=3.7',
        'reportlab': 'reportlab>=4.0',
        'numpy': 'numpy>=1.24',
        'requests': 'requests>=2.31',
    }
    for mod, pkg in packages.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', pkg])

    try:
        from importlib.metadata import version as _pkg_version
        raw = _pkg_version('kaggle')
        nums = [int(x) for x in re.findall(r'\d+', raw)[:3]]
        while len(nums) < 3:
            nums.append(0)
        kaggle_ok = tuple(nums) >= (2, 2, 4)
    except Exception:
        kaggle_ok = False
    if not kaggle_ok:
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', 'kaggle>=2.2.4'
        ])


ensure_dependencies()

# ---------------------------------------------------------------------------
# Kaggle authentication: reproduce the working pattern of the original bot.
# Credentials/files are created BEFORE importing KaggleApi.
# ---------------------------------------------------------------------------
def _prepare_original_kaggle_auth():
    if not KAGGLE_USERNAME:
        return {
            'username': '', 'modern_token': '', 'legacy_key': '', 'paths': []
        }

    raw_token = (KAGGLE_API_TOKEN or '').strip()
    raw_key = (KAGGLE_KEY or '').strip()

    modern_token = ''
    legacy_key = ''

    # A KGAT token may have been placed in either Render variable.
    for value in (raw_token, raw_key):
        if value.startswith('KGAT_') and len(value) > 5:
            modern_token = modern_token or value
            suffix = value[5:]
            if re.fullmatch(r'[0-9a-fA-F]{32}', suffix):
                legacy_key = legacy_key or suffix

    # A bare 32-hex value is the legacy Kaggle key used by the original bot.
    for value in (raw_key, raw_token):
        if re.fullmatch(r'[0-9a-fA-F]{32}', value or ''):
            legacy_key = legacy_key or value

    # If KAGGLE_API_TOKEN contains a non-KGAT modern token, preserve it exactly.
    if raw_token and not modern_token and not re.fullmatch(r'[0-9a-fA-F]{32}', raw_token):
        modern_token = raw_token

    os.environ['KAGGLE_USERNAME'] = KAGGLE_USERNAME
    if modern_token:
        os.environ['KAGGLE_API_TOKEN'] = modern_token
    if legacy_key:
        os.environ['KAGGLE_KEY'] = legacy_key

    written=[]
    home = os.environ.get('HOME') or str(Path.home())

    # Exact path style used by the uploaded original bot.
    config_dir = os.path.join(home, '.config', 'kaggle')
    os.makedirs(config_dir, exist_ok=True)
    if legacy_key:
        p = os.path.join(config_dir, 'kaggle.json')
        Path(p).write_text(
            json.dumps({'username': KAGGLE_USERNAME, 'key': legacy_key}),
            encoding='utf-8'
        )
        try: os.chmod(p, 0o600)
        except OSError: pass
        written.append(p)

    # Compatibility path used by many Kaggle API releases on Linux/Render.
    classic_dir = os.path.join(home, '.kaggle')
    os.makedirs(classic_dir, exist_ok=True)
    if legacy_key:
        p = os.path.join(classic_dir, 'kaggle.json')
        Path(p).write_text(
            json.dumps({'username': KAGGLE_USERNAME, 'key': legacy_key}),
            encoding='utf-8'
        )
        try: os.chmod(p, 0o600)
        except OSError: pass
        written.append(p)

    # Current Kaggle CLI/API token source. It does not replace the legacy JSON;
    # both are intentionally available, matching the original bot's dual setup.
    if modern_token:
        p = os.path.join(classic_dir, 'access_token')
        Path(p).write_text(modern_token, encoding='utf-8')
        try: os.chmod(p, 0o600)
        except OSError: pass
        written.append(p)

    return {
        'username': KAGGLE_USERNAME,
        'modern_token': modern_token,
        'legacy_key': legacy_key,
        'paths': written,
    }


KAGGLE_AUTH_INFO = _prepare_original_kaggle_auth()

# IMPORTANT: KaggleApi is imported only after the username/token/key and
# credential files above already exist, exactly as requested.
import telebot
from telebot import types
from kaggle.api.kaggle_api_extended import KaggleApi

if not BOT_TOKEN:
    raise RuntimeError("Set CHEMBOT_BOT_TOKEN before running the bot.")

_DEFAULT_ALLOWED_IDS = {7495822836, -1003907097817, 839801823, -1003925918657, -1003875125323}

def _parse_id_set(raw_value, fallback, variable_name):
    raw_value = str(raw_value or '').strip()
    if not raw_value:
        return set(fallback)
    try:
        ids = {int(token.strip()) for token in re.split(r'[,;\s]+', raw_value) if token.strip()}
    except ValueError as exc:
        raise RuntimeError(f'{variable_name} must contain comma-separated Telegram numeric IDs.') from exc
    if not ids:
        raise RuntimeError(f'{variable_name} was set but did not contain any Telegram numeric IDs.')
    return ids


ALLOWED_IDS = _parse_id_set(os.environ.get('CHEMBOT_ALLOWED_IDS'), _DEFAULT_ALLOWED_IDS, 'CHEMBOT_ALLOWED_IDS')
try:
    ADMIN_ID = int(os.environ.get('CHEMBOT_ADMIN_ID', '839801823'))
except ValueError as exc:
    raise RuntimeError('CHEMBOT_ADMIN_ID must be a Telegram numeric ID.') from exc
ORCA_DATASET_SLUG = os.environ.get(
    "ORCA_DATASET_SLUG", "abdulsalsmsalih/orca-6-1-0"
)
GAUSSIAN_DATASET_SLUG = os.environ.get(
    "GAUSSIAN_DATASET_SLUG",
    f"{KAGGLE_USERNAME or 'abdulsalsmsalih'}/gauusian16",
)
MAX_TELEGRAM_DOWNLOAD = 20 * 1024 * 1024
MAX_AUX_STORAGE = 20 * 1024 * 1024
MAX_TEXT_OUT = 20 * 1024 * 1024

user_aux_storage = {}
user_drive_links = {}
analysis_sessions = {}
analysis_group_batches = {}
analysis_group_lock = threading.RLock()
ANALYSIS_GROUP_DEBOUNCE_SECONDS = 2.5
# Protect short-lived shared state used by Telegram handler threads.
session_lock = threading.RLock()
state_lock = threading.RLock()
render_lock = threading.RLock()  # Matplotlib/ReportLab rendering is serialized for thread safety.

# Telegram can deliver several documents almost simultaneously.  Use enough
# worker threads to accept a batch, while every Kaggle submission receives its
# own KaggleApi instance and its own temporary directory.
try:
    _requested_worker_threads = int(os.environ.get('CHEMBOT_WORKER_THREADS', '8'))
except ValueError as exc:
    raise RuntimeError('CHEMBOT_WORKER_THREADS must be an integer from 2 to 32.') from exc
BOT_WORKER_THREADS = min(32, max(2, _requested_worker_threads))


def detect_job_engine(filename, text=""):
    """Identify calculation inputs from their extension and Gaussian route card."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    suffix = Path(name).suffix
    if suffix == ".dat":
        return "psi4"
    if suffix in {".gjf", ".com", ".gau"}:
        return "gaussian"
    if suffix == ".inp":
        contents = str(text or "")
        # ORCA comments also begin with '#'. An ORCA command/geometry wins
        # over a comment that happens to mention Gaussian-like keywords.
        if (re.search(r'(?im)^\s*!\s*\S', contents) or
            re.search(r'(?im)^\s*%\s*(?:pal|maxcore|scf|geom|tddft|cpcm|basis|freq)\b', contents) or
            re.search(r'(?im)^\s*\*\s*(?:xyz|xyzfile|int)\b', contents)):
            return "orca"
        # Only route-like cards identify a Gaussian input with .inp suffix.
        if re.search(r'(?im)^\s*#\s*(?:[PNT](?:\s|$)|[A-Za-z][\w()+.\-]*/[\w()+.*\-]+|(?:opt|freq|td|sp)\b)', contents):
            return "gaussian"
        return "orca"
    return None


def decode_job_input(raw):
    """Decode common GaussView-on-Windows text encodings and normalize newlines."""
    data = bytes(raw)
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        text = data.decode('utf-32')
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode('utf-16')
    elif data.startswith(codecs.BOM_UTF8):
        text = data.decode('utf-8-sig')
    else:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            text = data.decode('cp1256')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if '\x00' in text:
        raise ValueError('The input contains NUL characters and is not a supported Gaussian text file.')
    return text

# Names used by older ChemBot versions.  They are safe to prune when they are
# clearly local staging directories, because the Kaggle kernel has already
# received a copy after kernels_push() returns.
LEGACY_JOB_PREFIXES = ("orca-job-", "psi4-job-", "chem-job-")


def _safe_rmtree(path):
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def cleanup_stale_local_job_dirs():
    """Remove staging directories left by older/interrupted launcher runs.

    Current-working-directory legacy jobs are removed immediately.  Temporary
    directory jobs are removed only when older than one hour so another live
    launcher process is not disturbed.
    """
    removed = []
    now = time.time()
    roots = [(Path.cwd(), 0.0), (Path(tempfile.gettempdir()), 3600.0)]
    seen = set()
    for root, min_age in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if str(root) in seen:
            continue
        seen.add(str(root))
        try:
            entries = list(root.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or not entry.name.startswith(LEGACY_JOB_PREFIXES):
                    continue
                # Restrict deletion to the exact naming family generated by the bot.
                suffix = entry.name.split('-', 2)[-1]
                if not re.fullmatch(r'[A-Za-z0-9_]+', suffix or ''):
                    continue
                age = max(0.0, now - entry.stat().st_mtime)
                if age < min_age:
                    continue
                _safe_rmtree(str(entry))
                if not entry.exists():
                    removed.append(str(entry))
            except Exception:
                continue
    return removed


def snapshot_user_job_context(user_id):
    """Return independent copies of auxiliary files and restart URL.

    The snapshot is intentionally non-destructive so a burst of .inp/.dat
    messages from the same user can all use the same endpoint/restart files.
    /clearaux or /start explicitly clears the shared preparation context.
    """
    with state_lock:
        aux = dict(user_aux_storage.get(user_id, {}))
        drive = str(user_drive_links.get(user_id, '') or '')
    return aux, drive

# ============================================================
# Shared analyzer source. It is executed locally and injected into Kaggle jobs.
# ============================================================
ANALYZER_MODULE_CODE = r'''
import os, re, math, json, textwrap, hashlib
from pathlib import Path

REPORT_GENERATOR_VERSION = '6.2.4'
HARTREE_TO_KJMOL = 2625.499638
HARTREE_TO_EV = 27.211386245988
KB_J_MOL_K = 8.314462618


def _float(x):
    try:
        return float(str(x).replace('D','E').replace('d','e'))
    except Exception:
        return None


def _last_number(text, patterns, flags=re.I | re.M):
    for pat in patterns:
        m = list(re.finditer(pat, text, flags))
        if m:
            v = _float(m[-1].group(1))
            if v is not None:
                return v
    return None


def detect_engine(text):
    u = text.upper()
    if 'O   R   C   A' in u or 'ORCA TERMINATED NORMALLY' in u or 'ORCA VERSION' in u:
        return 'ORCA'
    if 'PSI4' in u or 'PSITHON' in u or 'AN OPEN-SOURCE AB INITIO ELECTRONIC STRUCTURE PACKAGE' in u:
        return 'Psi4'
    return 'Unknown'


def detect_version(text, engine):
    pats = []
    if engine == 'ORCA':
        pats = [r'Program Version\s+([0-9][\w.\-]+)', r'ORCA\s+VERSION\s*[:=]?\s*([0-9][\w.\-]+)']
    elif engine == 'Psi4':
        pats = [r'Psi4\s+([0-9][\w.\-]+)', r'Psi4\s+Version\s*[:=]?\s*([0-9][\w.\-]+)']
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None


def parse_method_basis(text, engine):
    method = basis = None
    if engine == 'ORCA':
        for p in [r'Exchange Functional\s+.*?\b([A-Za-z0-9+()\-ω]+)\b\s*$',
                  r'Method\s*:\s*([^\n]+)', r'!\s*([^\n]+)']:
            m = re.search(p, text, re.I | re.M)
            if m:
                candidate = m.group(1).strip()
                if p.startswith('!'):
                    toks = candidate.split()
                    method_tokens = [t for t in toks if re.search(r'(B3LYP|PBE|M06|WB97|ωB97|CAM|HF|MP2|DLPNO|CCSD|R2SCAN|B97|TPSSh|BP86)', t, re.I)]
                    basis_tokens = [t for t in toks if re.search(r'(def2|cc-p|aug-|6-31|6-311|ma-|pcseg|ano)', t, re.I)]
                    if method_tokens and not method: method = method_tokens[0]
                    if basis_tokens and not basis: basis = basis_tokens[0]
                elif not method:
                    method = candidate[:100]
        m = re.search(r'(?:basis set|Basis)\s*(?:=|:)\s*([^\n]+)', text, re.I)
        if m and not basis: basis = m.group(1).strip()[:100]
    else:
        m = re.search(r'energy\s*\(\s*[\'\"]([^\'\"]+)', text, re.I)
        if m: method = m.group(1)
        m = re.search(r'\bbasis\s+([A-Za-z0-9+*()_\-]+)', text, re.I)
        if m: basis = m.group(1)
        if not method:
            m = re.search(r'==>\s*([A-Za-z0-9+()_\-]+)\s*<==', text)
            if m: method = m.group(1)
    return method, basis


def parse_status(text, engine):
    u = text.upper()
    normal = False
    if engine == 'ORCA': normal = 'ORCA TERMINATED NORMALLY' in u
    if engine == 'Psi4': normal = ('PSI4 EXITING SUCCESSFULLY' in u or '*** PSI4 EXITING SUCCESSFULLY' in u)
    error_lines = []
    for line in text.splitlines():
        lu = line.upper()
        if any(k in lu for k in ['ERROR', 'FATAL', 'ABORT', 'SEGMENTATION FAULT', 'OUT OF MEMORY', 'SCF NOT CONVERGED', 'FAILED TO CONVERGE']):
            if len(line.strip()) > 3:
                error_lines.append(line.strip())
    # Deduplicate preserving order
    seen, dedup = set(), []
    for x in error_lines:
        if x not in seen:
            seen.add(x); dedup.append(x)
    return normal, dedup[-20:]


def parse_energies(text, engine):
    result = {}
    patterns = {
        'final_energy_hartree': [
            r'FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)',
            r'Final Energy\s*:\s*(-?\d+\.\d+)',
            r'Total Energy\s*=\s*(-?\d+\.\d+)\s*(?:Eh|a\.u\.)?',
            r'\*\s+CCSD\(T\) total energy\s*=\s*(-?\d+\.\d+)',
        ],
        'scf_energy_hartree': [r'Total Energy\s*:\s*(-?\d+\.\d+)\s*Eh', r'@DF-RKS Final Energy:\s*(-?\d+\.\d+)'],
        'dispersion_energy_hartree': [r'Dispersion correction\s+(-?\d+\.\d+)', r'DISPERSION CORRECTION ENERGY\s*=\s*(-?\d+\.\d+)'],
        'correlation_energy_hartree': [r'Correlation Energy\s*[:=]\s*(-?\d+\.\d+)'],
        'nuclear_repulsion_hartree': [r'Nuclear Repulsion Energy\s*[:=]\s*(-?\d+\.\d+)'],
    }
    for key, pats in patterns.items():
        v = _last_number(text, pats)
        if v is not None: result[key] = v
    if 'final_energy_hartree' in result:
        e = result['final_energy_hartree']
        result['final_energy_kj_mol'] = e * HARTREE_TO_KJMOL
        result['final_energy_ev'] = e * HARTREE_TO_EV
    return result


def parse_optimization(text, engine):
    vals = []
    if engine == 'ORCA':
        vals = [_float(x) for x in re.findall(r'FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)', text, re.I)]
        if len(vals) < 2:
            vals = [_float(x) for x in re.findall(r'\bE\(SCF\)\s*=\s*(-?\d+\.\d+)', text, re.I)]
    else:
        vals = [_float(x) for x in re.findall(r'\b(?:Total Energy|Energy)\s*=\s*(-?\d+\.\d+)', text, re.I)]
        if len(vals) < 2:
            vals = [_float(x) for x in re.findall(r'@(?:DF-)?(?:RHF|RKS|UHF|UKS) Final Energy:\s*(-?\d+\.\d+)', text, re.I)]
    vals = [v for v in vals if v is not None]
    # Compress exact sequential duplicates
    out = []
    for v in vals:
        if not out or abs(v-out[-1]) > 1e-12: out.append(v)
    return out[-500:]


def parse_thermo(text, engine):
    t = {}
    fields = {
        'temperature_K': [r'Temperature\s*(?:\.\.\.|:|=)?\s*([0-9]+\.?[0-9]*)\s*K'],
        'zpe_hartree': [r'Zero point energy\s*(?:\.\.\.|:|=)?\s*(-?\d+\.\d+)\s*(?:Eh|Hartree)', r'Zero-point vibrational energy\s*[:=]\s*(-?\d+\.\d+)'],
        'thermal_energy_hartree': [r'Total thermal energy\s*(?:\.\.\.|:|=)?\s*(-?\d+\.\d+)\s*Eh', r'Thermal correction to Energy\s*=\s*(-?\d+\.\d+)'],
        'enthalpy_hartree': [r'Total Enthalpy\s*(?:\.\.\.|:|=)?\s*(-?\d+\.\d+)\s*Eh', r'Enthalpy\s*[:=]\s*(-?\d+\.\d+)'],
        'gibbs_hartree': [r'Final Gibbs free energy\s*(?:\.\.\.|:|=)?\s*(-?\d+\.\d+)\s*Eh', r'Gibbs free energy\s*[:=]\s*(-?\d+\.\d+)', r'Total Gibbs free energy\s*[:=]\s*(-?\d+\.\d+)'],
        'entropy_hartree_per_K': [r'Total entropy correction\s*(?:\.\.\.|:|=)?\s*(-?\d+\.\d+)\s*Eh/K'],
        'entropy_J_mol_K': [r'(?:Total\s+)?Entropy\s*(?:\.\.\.|:|=)?\s*(-?\d+\.?\d*)\s*J\s*/?\s*mol(?:e)?\s*\*?\s*K', r'Entropy\s*=\s*(-?\d+\.?\d*)\s*\[J/mol/K\]'],
    }
    for k,p in fields.items():
        v = _last_number(text,p)
        if v is not None: t[k]=v
    for k in ['zpe_hartree','thermal_energy_hartree','enthalpy_hartree','gibbs_hartree']:
        if k in t: t[k.replace('_hartree','_kj_mol')] = t[k]*HARTREE_TO_KJMOL
    if 'entropy_hartree_per_K' in t and 'entropy_J_mol_K' not in t:
        t['entropy_J_mol_K'] = t['entropy_hartree_per_K'] * HARTREE_TO_KJMOL * 1000.0
    return t


def parse_frequencies(text, engine):
    freqs = []
    # ORCA common forms: "  7:   123.45 cm**-1" and Psi4 "Freq [cm^-1] ..."
    for m in re.finditer(r'^\s*\d+\s*:\s*(-?\d+(?:\.\d+)?)\s*cm\*\*-1', text, re.I|re.M):
        freqs.append(_float(m.group(1)))
    if not freqs:
        for line in text.splitlines():
            if re.search(r'Freq(?:uency|\s*\[cm)', line, re.I):
                nums = re.findall(r'(?<!\w)-?\d+(?:\.\d+)?', line)
                for n in nums:
                    v=_float(n)
                    if v is not None and -10000 < v < 10000: freqs.append(v)
    # IR intensities from ORCA IR SPECTRUM tables
    ir = []
    in_ir = False
    for line in text.splitlines():
        if 'IR SPECTRUM' in line.upper(): in_ir=True; continue
        if in_ir and ('RAMAN SPECTRUM' in line.upper() or 'THERMOCHEMISTRY' in line.upper()): in_ir=False
        if in_ir:
            nums = re.findall(r'-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?', line)
            if len(nums) >= 3:
                vals=[_float(x) for x in nums]
                # typically mode, frequency, epsilon/intensity
                if vals[1] is not None and -10000 < vals[1] < 10000:
                    intensity = vals[-1] if vals[-1] is not None else 0.0
                    ir.append((vals[1], intensity))
    # If no IR table, retain frequency-only sticks
    if not ir and freqs:
        ir=[(f,1.0) for f in freqs]
    # reasonable de-duplication
    clean=[]
    for f in freqs:
        if f is not None and (not clean or abs(f-clean[-1])>1e-6): clean.append(f)
    return clean[:2000], ir[:2000]


def parse_tddft(text, engine):
    states=[]
    # ORCA: STATE  1: E=   0.123 au      3.35 eV    370.1 nm  f=0.123
    p1=re.compile(r'STATE\s+(\d+)\s*:\s*E\s*=.*?([0-9]+\.?[0-9]*)\s*eV.*?([0-9]+\.?[0-9]*)\s*nm.*?f\s*=\s*([0-9.Ee+\-]+)', re.I)
    for m in p1.finditer(text):
        states.append({'state':int(m.group(1)),'ev':_float(m.group(2)),'nm':_float(m.group(3)),'f':_float(m.group(4)) or 0.0})
    # ORCA absorption spectrum: energy(cm-1), wavelength, fosc variants
    if not states:
        for line in text.splitlines():
            if re.search(r'\bSTATE\s+\d+', line, re.I) and 'EV' in line.upper():
                sm=re.search(r'STATE\s+(\d+)',line,re.I); evm=re.search(r'([0-9]+\.?[0-9]*)\s*eV',line,re.I); nmm=re.search(r'([0-9]+\.?[0-9]*)\s*nm',line,re.I); fm=re.search(r'f\s*=\s*([0-9.Ee+\-]+)',line,re.I)
                if sm and evm:
                    ev=_float(evm.group(1)); nm=_float(nmm.group(1)) if nmm else (1239.841984/ev if ev else None)
                    states.append({'state':int(sm.group(1)),'ev':ev,'nm':nm,'f':_float(fm.group(1)) if fm else 0.0})
    # Psi4 common excited-state lines
    if not states:
        for m in re.finditer(r'(?:Excited state|State)\s+(\d+).*?([0-9]+\.?[0-9]*)\s*eV(?:.*?([0-9]+\.?[0-9]*)\s*nm)?(?:.*?(?:oscillator strength|f)\s*[=:]\s*([0-9.Ee+\-]+))?', text, re.I):
            ev=_float(m.group(2)); nm=_float(m.group(3)) if m.group(3) else (1239.841984/ev if ev else None)
            states.append({'state':int(m.group(1)),'ev':ev,'nm':nm,'f':_float(m.group(4)) if m.group(4) else 0.0})
    # de-dup state/nm
    out=[]; seen=set()
    for s in states:
        key=(s['state'], round(s.get('nm') or 0,4))
        if key not in seen:
            seen.add(key); out.append(s)
    return out[:1000]


def parse_orbitals(text, engine):
    orbitals=[]
    # ORCA orbital tables: NO OCC E(Eh) E(eV)
    in_tbl=False
    for line in text.splitlines():
        u=line.upper()
        if 'ORBITAL ENERGIES' in u: in_tbl=True; continue
        if in_tbl and ('MOLECULAR ORBITALS' in u or 'SPIN UP ORBITALS' in u or 'MULLIKEN' in u):
            if orbitals: break
        if in_tbl:
            m=re.match(r'^\s*(\d+)\s+([0-9.]+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)',line)
            if m:
                orbitals.append({'index':int(m.group(1)),'occ':_float(m.group(2)),'eh':_float(m.group(3)),'ev':_float(m.group(4))})
    homo=lumo=None
    if orbitals:
        occ=[o for o in orbitals if (o['occ'] or 0)>1e-6]
        vir=[o for o in orbitals if (o['occ'] or 0)<=1e-6]
        if occ: homo=occ[-1]
        if vir: lumo=vir[0]
    if not homo:
        hv=_last_number(text,[r'HOMO\s*(?:energy)?\s*[:=]\s*(-?\d+\.\d+)\s*eV'])
        if hv is not None: homo={'ev':hv}
    if not lumo:
        lv=_last_number(text,[r'LUMO\s*(?:energy)?\s*[:=]\s*(-?\d+\.\d+)\s*eV'])
        if lv is not None: lumo={'ev':lv}
    res={'orbitals':orbitals[-500:]}
    if homo: res['homo_ev']=homo.get('ev')
    if lumo: res['lumo_ev']=lumo.get('ev')
    if homo and lumo and homo.get('ev') is not None and lumo.get('ev') is not None:
        res['gap_ev']=lumo['ev']-homo['ev']
    return res


def parse_dipole(text):
    res={}
    # ORCA magnitude
    mag=_last_number(text,[r'Magnitude \(Debye\)\s*:\s*([0-9.]+)',r'Dipole Moment\s*[:=].*?([0-9.]+)\s*Debye'])
    if mag is not None: res['magnitude_debye']=mag
    m=re.search(r'Total Dipole Moment\s*[:=]\s*\[?\s*(-?[0-9.]+)[, ]+(-?[0-9.]+)[, ]+(-?[0-9.]+)',text,re.I)
    if m:
        res['vector']=[_float(m.group(1)),_float(m.group(2)),_float(m.group(3))]
    return res


def parse_charge_mult(text):
    charge=_last_number(text,[r'Total Charge\s*(?:Charge)?\s*[:=]\s*(-?\d+)', r'charge\s*=\s*(-?\d+)'])
    mult=_last_number(text,[r'Multiplicity\s*[:=]\s*(\d+)',r'multiplicity\s*=\s*(\d+)'])
    return {'charge':int(charge) if charge is not None else None,'multiplicity':int(mult) if mult is not None else None}


def parse_atomic_charges(text):
    charges=[]
    active=False
    for line in text.splitlines():
        u=line.upper()
        if 'MULLIKEN ATOMIC CHARGES' in u or 'MULLIKEN CHARGES' in u:
            active=True; charges=[]; continue
        if active and ('SUM OF ATOMIC CHARGES' in u or 'LOEWDIN ATOMIC CHARGES' in u or 'MAYER POPULATION' in u):
            if charges: break
        if active:
            m=re.match(r'^\s*(\d+)\s+([A-Za-z]{1,3})\s*:?\s*(-?\d+\.\d+)',line)
            if m: charges.append({'index':int(m.group(1)),'element':m.group(2),'charge':_float(m.group(3))})
    return charges[-1000:]


def parse_final_geometry(text):
    blocks=[]; current=[]; active=False
    for line in text.splitlines():
        u=line.upper()
        if 'CARTESIAN COORDINATES (ANGSTROEM)' in u or 'CARTESIAN COORDINATES (ANGSTROM)' in u:
            if current: blocks.append(current)
            current=[]; active=True; continue
        if active:
            if not line.strip() or set(line.strip()) <= {'-'}:
                if current:
                    blocks.append(current); current=[]; active=False
                continue
            m=re.match(r'^\s*([A-Za-z]{1,3})\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)',line)
            if m: current.append({'element':m.group(1),'x':_float(m.group(2)),'y':_float(m.group(3)),'z':_float(m.group(4))})
    if current: blocks.append(current)
    return blocks[-1] if blocks else []


def parse_raman(text):
    data=[]; active=False
    for line in text.splitlines():
        u=line.upper()
        if 'RAMAN SPECTRUM' in u: active=True; continue
        if active and any(k in u for k in ['THERMOCHEMISTRY','NORMAL MODES','IR SPECTRUM']):
            if data: break
        if active:
            nums=re.findall(r'-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?',line)
            if len(nums)>=3:
                vals=[_float(x) for x in nums]
                if vals[1] is not None and -10000 < vals[1] < 10000:
                    data.append((vals[1], abs(vals[-1] or 0.0)))
    return data[:2000]


def section_structure(a):
    lines=[]
    sys=a.get('system',{})
    if sys.get('charge') is not None: lines.append(f"Charge: {sys['charge']}")
    if sys.get('multiplicity') is not None: lines.append(f"Multiplicity: {sys['multiplicity']}")
    d=a.get('dipole',{})
    if d.get('magnitude_debye') is not None: lines.append(f"Dipole magnitude: {d['magnitude_debye']:.8f} D")
    ch=a.get('atomic_charges',[])
    if ch:
        lines.append('\nMulliken atomic charges:')
        lines.append('Index | Atom | Charge')
        for x in ch[:300]: lines.append(f"{x['index']} | {x['element']} | {x['charge']:.8f}")
    g=a.get('final_geometry',[])
    if g:
        lines.append('\nFinal Cartesian geometry (Angstrom):')
        for x in g[:500]: lines.append(f"{x['element']:>2s}  {x['x']: .8f}  {x['y']: .8f}  {x['z']: .8f}")
    return '\n'.join(lines) if lines else 'No charge/dipole/geometry section was recognized.'


def parse_timings(text, engine):
    res={}
    m=re.search(r'TOTAL RUN TIME:\s*(\d+)\s*days\s*(\d+)\s*hours\s*(\d+)\s*minutes\s*([0-9.]+)\s*seconds',text,re.I)
    if m:
        d,h,mi,s=map(float,m.groups()); res['wall_seconds']=d*86400+h*3600+mi*60+s
    else:
        v=_last_number(text,[r'Total time\s*=\s*([0-9.]+)\s*seconds',r'Wall Time\s*[:=]\s*([0-9.]+)'])
        if v is not None: res['wall_seconds']=v
    return res


def parse_output_text(text, filename='calculation.out'):
    engine=detect_engine(text)
    normal, errors=parse_status(text,engine)
    method,basis=parse_method_basis(text,engine)
    freqs,ir=parse_frequencies(text,engine)
    td=parse_tddft(text,engine)
    orb=parse_orbitals(text,engine)
    out={
        'filename':filename,
        'engine':engine,
        'version':detect_version(text,engine),
        'normal_termination':normal,
        'errors':errors,
        'method':method,
        'basis':basis,
        'energies':parse_energies(text,engine),
        'optimization_energies':parse_optimization(text,engine),
        'thermochemistry':parse_thermo(text,engine),
        'frequencies_cm1':freqs,
        'imaginary_frequencies_cm1':[x for x in freqs if x < -1e-6],
        'ir_spectrum':ir,
        'tddft_states':td,
        'orbitals':orb,
        'dipole':parse_dipole(text),
        'atomic_charges':parse_atomic_charges(text),
        'final_geometry':parse_final_geometry(text),
        'raman_spectrum':parse_raman(text),
        'system':parse_charge_mult(text),
        'timings':parse_timings(text,engine),
    }
    return out


def fmt(v, digits=8):
    if v is None: return 'N/A'
    if isinstance(v,float): return f'{v:.{digits}g}'
    return str(v)


def section_summary(a):
    e=a.get('energies',{}); o=a.get('orbitals',{}); t=a.get('thermochemistry',{})
    lines=[f"Engine: {a.get('engine')} {a.get('version') or ''}".strip(),
           f"Status: {'NORMAL TERMINATION' if a.get('normal_termination') else 'NOT CONFIRMED'}",
           f"Method: {a.get('method') or 'N/A'}", f"Basis: {a.get('basis') or 'N/A'}"]
    if 'final_energy_hartree' in e: lines.append(f"Final energy: {e['final_energy_hartree']:.12f} Eh")
    if o.get('homo_ev') is not None: lines.append(f"HOMO: {o['homo_ev']:.6f} eV")
    if o.get('lumo_ev') is not None: lines.append(f"LUMO: {o['lumo_ev']:.6f} eV")
    if o.get('gap_ev') is not None: lines.append(f"HOMO-LUMO gap: {o['gap_ev']:.6f} eV")
    if a.get('frequencies_cm1'): lines.append(f"Vibrational modes parsed: {len(a['frequencies_cm1'])}; imaginary: {len(a.get('imaginary_frequencies_cm1',[]))}")
    if a.get('tddft_states'): lines.append(f"Excited states parsed: {len(a['tddft_states'])}")
    if t.get('gibbs_hartree') is not None: lines.append(f"Gibbs free energy: {t['gibbs_hartree']:.12f} Eh")
    if a.get('errors'): lines.append('Diagnostics: '+a['errors'][-1][:300])
    return '\n'.join(lines)


def section_energies(a):
    e=a.get('energies',{})
    if not e: return 'No energy values were recognized.'
    return '\n'.join(f"{k}: {fmt(v,12)}" for k,v in e.items())


def section_thermo(a, which='all'):
    t=a.get('thermochemistry',{})
    if not t: return 'No thermochemistry block was recognized in this output.'
    keys=list(t)
    filters={'zpe':['zpe'],'h':['enthalpy','thermal_energy','temperature'],'g':['gibbs','temperature'],'s':['entropy','temperature']}
    if which in filters:
        keys=[k for k in keys if any(x in k for x in filters[which])]
    return '\n'.join(f"{k}: {fmt(t[k],12)}" for k in keys) if keys else 'Requested thermochemical quantity was not found.'


def section_vibrations(a):
    f=a.get('frequencies_cm1',[]); im=a.get('imaginary_frequencies_cm1',[])
    if not f: return 'No vibrational frequencies were recognized.'
    head=f"Modes: {len(f)}\nImaginary modes: {len(im)}"
    if im: head += '\nImaginary frequencies (cm^-1): ' + ', '.join(f'{x:.3f}' for x in im[:30])
    head += '\n\nFrequencies (cm^-1):\n' + ', '.join(f'{x:.3f}' for x in f[:500])
    return head


def section_uv(a):
    s=a.get('tddft_states',[])
    if not s: return 'No TD-DFT/UV-Vis excited states were recognized.'
    lines=['State | eV | nm | f']
    for x in s[:300]: lines.append(f"{x['state']} | {fmt(x.get('ev'),7)} | {fmt(x.get('nm'),7)} | {fmt(x.get('f'),7)}")
    return '\n'.join(lines)


def section_orbitals(a):
    o=a.get('orbitals',{})
    lines=[]
    for k in ['homo_ev','lumo_ev','gap_ev']:
        if o.get(k) is not None: lines.append(f'{k}: {o[k]:.8f} eV')
    arr=o.get('orbitals',[])
    if arr:
        lines.append('\nIndex | Occ | E(Eh) | E(eV)')
        for x in arr[-80:]: lines.append(f"{x.get('index')} | {fmt(x.get('occ'),5)} | {fmt(x.get('eh'),8)} | {fmt(x.get('ev'),8)}")
    return '\n'.join(lines) if lines else 'No orbital-energy table was recognized.'


def section_diagnostics(a):
    lines=[f"Normal termination: {a.get('normal_termination')}"]
    if a.get('timings'): lines += [f"{k}: {v}" for k,v in a['timings'].items()]
    if a.get('errors'):
        lines.append('\nDetected warning/error lines:')
        lines += a['errors']
    else: lines.append('No fatal/error keyword lines detected by the parser.')
    return '\n'.join(lines)


def make_plots(a, outdir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    os.makedirs(outdir,exist_ok=True)
    made={}

    def _gaussian_broaden(xvals, yvals, grid, sigma):
        yy = np.zeros_like(grid, dtype=float)
        if sigma <= 0:
            sigma = 1.0
        for xv, yv in zip(xvals, yvals):
            if xv is None or yv is None:
                continue
            amp = max(0.0, float(yv))
            yy += amp * np.exp(-0.5 * ((grid - float(xv)) / sigma) ** 2)
        return yy

    opt=a.get('optimization_energies',[])
    if len(opt)>=2:
        p=os.path.join(outdir,'optimization_energy.png')
        arr = np.array([float(v) for v in opt if v is not None], dtype=float)
        if arr.size >= 2:
            rel = (arr - np.min(arr)) * HARTREE_TO_KJMOL
            fig=plt.figure(figsize=(8,5)); ax=fig.add_subplot(111)
            ax.plot(range(1,len(rel)+1), rel, marker='o', ms=3)
            ax.set_xlabel('Optimization step')
            ax.set_ylabel('Relative energy (kJ mol$^{-1}$)')
            ax.set_title('Optimization energy profile')
            ax.grid(alpha=.25)
            ymin = min(rel.min(), 0.0)
            ymax = rel.max() if rel.size else 1.0
            pad = max(1.0, 0.05 * (ymax - ymin if ymax > ymin else 1.0))
            ax.set_ylim(max(-pad, ymin - pad), ymax + pad)
            fig.tight_layout(); fig.savefig(p,dpi=300,bbox_inches='tight'); plt.close(fig); made['optimization']=p

    ir=a.get('ir_spectrum',[])
    if ir:
        p=os.path.join(outdir,'ir_spectrum.png')
        pairs=[]
        for q in ir:
            try:
                freq=float(q[0]); inten=abs(float(q[1] or 0.0))
            except Exception:
                continue
            if freq > 0 and inten >= 0:
                pairs.append((freq, inten))
        # Prefer conventional mid-IR presentation (4000–400 cm^-1).
        mid_ir=[(f,i) for f,i in pairs if 400.0 <= f <= 4000.0]
        use = mid_ir if mid_ir else pairs
        if use:
            x=np.array([f for f,_ in use], dtype=float)
            y=np.array([i for _,i in use], dtype=float)
            if np.max(y) > 0:
                y = y / np.max(y)
            lo = 400.0 if mid_ir else max(0.0, np.min(x) - 120.0)
            hi = 4000.0 if mid_ir else float(np.max(x) + 120.0)
            if hi <= lo:
                lo, hi = float(np.min(x)), float(np.max(x))
            grid=np.linspace(lo, hi, 3600)
            yy=_gaussian_broaden(x, y, grid, sigma=16.0)
            if np.max(yy) > 0:
                yy = yy / np.max(yy)
            trans = 100.0 - 92.0 * yy
            sticks = 100.0 - 35.0 * y
            fig=plt.figure(figsize=(9,5)); ax=fig.add_subplot(111)
            ax.plot(grid, trans, lw=1.8)
            ax.vlines(x, 100.0, sticks, alpha=.12, linewidth=0.6)
            ax.set_xlabel('Wavenumber (cm$^{-1}$)')
            ax.set_ylabel('Transmittance (%)')
            ax.set_title('Simulated FT-IR spectrum')
            ax.set_xlim(hi, lo)
            ax.set_ylim(0, 102)
            ax.grid(alpha=.18)
            fig.tight_layout(); fig.savefig(p,dpi=300,bbox_inches='tight'); plt.close(fig); made['ir']=p

    states=[s for s in a.get('tddft_states',[]) if s.get('nm') and s.get('f') is not None]
    if states:
        p=os.path.join(outdir,'uvvis_spectrum.png')
        nms=np.array([float(s['nm']) for s in states if s.get('nm')], dtype=float)
        fosc=np.array([max(0.0, float(s.get('f') or 0.0)) for s in states if s.get('nm')], dtype=float)
        if nms.size:
            lo=max(100.0, float(np.min(nms))-80.0)
            hi=min(2000.0, float(np.max(nms))+80.0)
            grid=np.linspace(lo,hi,2500)
            yy=_gaussian_broaden(nms, fosc, grid, sigma=10.0)
            if np.max(yy)>0: yy=yy/np.max(yy)
            stick = fosc / (np.max(fosc) if np.max(fosc)>0 else 1.0)
            fig=plt.figure(figsize=(9,5)); ax=fig.add_subplot(111)
            ax.plot(grid,yy,lw=1.6)
            ax.vlines(nms,0,stick,alpha=.35,linewidth=0.8)
            ax.set_xlabel('Wavelength (nm)')
            ax.set_ylabel('Relative intensity')
            ax.set_title('Simulated UV-Vis spectrum (Gaussian broadening)')
            ax.set_ylim(0, 1.05)
            ax.grid(alpha=.2)
            fig.tight_layout(); fig.savefig(p,dpi=300,bbox_inches='tight'); plt.close(fig); made['uvvis']=p

    raman=a.get('raman_spectrum',[])
    if raman:
        p=os.path.join(outdir,'raman_spectrum.png')
        pairs=[]
        for q in raman:
            try:
                shift=float(q[0]); act=abs(float(q[1] or 0.0))
            except Exception:
                continue
            if shift > 0 and act >= 0:
                pairs.append((shift, act))
        if pairs:
            x=np.array([f for f,_ in pairs], dtype=float)
            y=np.array([i for _,i in pairs], dtype=float)
            if np.max(y) > 0:
                y = y / np.max(y)
            lo = max(0.0, np.min(x) - 80.0)
            hi = np.max(x) + 80.0
            grid=np.linspace(lo, hi, 3200)
            yy=_gaussian_broaden(x, y, grid, sigma=10.0)
            if np.max(yy) > 0:
                yy = yy / np.max(yy)
            fig=plt.figure(figsize=(9,5)); ax=fig.add_subplot(111)
            ax.plot(grid, yy, lw=1.6)
            ax.vlines(x, 0, y, alpha=.20, linewidth=0.6)
            ax.set_xlabel('Raman shift (cm$^{-1}$)')
            ax.set_ylabel('Relative activity')
            ax.set_title('Calculated Raman spectrum')
            ax.set_xlim(hi, lo)
            ax.set_ylim(0, 1.05)
            ax.grid(alpha=.2)
            fig.tight_layout(); fig.savefig(p,dpi=300,bbox_inches='tight'); plt.close(fig); made['raman']=p

    o=a.get('orbitals',{}).get('orbitals',[])
    if o:
        p=os.path.join(outdir,'orbital_energies.png')
        cleaned=[z for z in o if z.get('ev') is not None]
        occ=[z for z in cleaned if (z.get('occ') or 0.0) > 1e-8]
        vir=[z for z in cleaned if (z.get('occ') or 0.0) <= 1e-8]
        show_occ = occ[-6:]
        show_vir = vir[:6]
        subset = show_occ + show_vir
        if subset:
            ys=[float(z.get('ev')) for z in subset]
            fig=plt.figure(figsize=(6.8,7.2)); ax=fig.add_subplot(111)

            def _spread_positions(yvals, min_sep=0.12):
                if not yvals:
                    return []
                out=[float(yvals[0])]
                for yv in yvals[1:]:
                    yv=float(yv)
                    if yv - out[-1] < min_sep:
                        yv = out[-1] + min_sep
                    out.append(yv)
                return out

            for z in show_occ:
                yv=float(z.get('ev'))
                lw=2.4 if z is occ[-1] else 1.4
                ax.hlines(yv, -0.34, -0.08, lw=lw)
            for z in show_vir:
                yv=float(z.get('ev'))
                lw=2.4 if z is vir[0] else 1.4
                ax.hlines(yv, 0.08, 0.34, lw=lw)
            homo = occ[-1] if occ else None
            lumo = vir[0] if vir else None
            if homo is not None:
                yv=float(homo.get('ev'))
                ax.text(-0.40, yv, f"HOMO\n{yv:.2f} eV", ha='right', va='center', fontsize=9)
            if lumo is not None:
                yv=float(lumo.get('ev'))
                ax.text(0.40, yv, f"LUMO\n{yv:.2f} eV", ha='left', va='center', fontsize=9)
            occ_lab = show_occ[-3:-1]
            vir_lab = show_vir[1:3]
            occ_text_y = _spread_positions([float(z.get('ev')) for z in occ_lab], min_sep=0.11)
            vir_text_y = _spread_positions([float(z.get('ev')) for z in vir_lab], min_sep=0.11)
            for z, yt in zip(occ_lab, occ_text_y):
                yv=float(z.get('ev'))
                ax.annotate(str(z.get('index')), xy=(-0.08, yv), xytext=(-0.02, yt), textcoords='data', ha='right', va='center', fontsize=7, arrowprops=dict(arrowstyle='-', lw=0.5, shrinkA=0, shrinkB=0))
            for z, yt in zip(vir_lab, vir_text_y):
                yv=float(z.get('ev'))
                ax.annotate(str(z.get('index')), xy=(0.08, yv), xytext=(0.02, yt), textcoords='data', ha='left', va='center', fontsize=7, arrowprops=dict(arrowstyle='-', lw=0.5, shrinkA=0, shrinkB=0))
            if homo is not None and lumo is not None:
                yh=float(homo.get('ev')); yl=float(lumo.get('ev'))
                gap = yl - yh
                xgap = 0.0
                ax.annotate('', xy=(xgap, yl), xytext=(xgap, yh), arrowprops=dict(arrowstyle='<->', lw=1.2))
                ax.text(xgap + 0.03, (yl+yh)/2.0, f'ΔE = {gap:.2f} eV', va='center', fontsize=8)
            ymin=min(ys); ymax=max(ys); pad=max(0.4, 0.10*(ymax-ymin if ymax>ymin else 1.0))
            ax.set_xlim(-0.5, 0.5)
            ax.set_ylim(ymin-pad, ymax+pad)
            ax.set_xticks([-0.2, 0.2]); ax.set_xticklabels(['Occupied', 'Virtual'])
            ax.set_ylabel('Orbital energy (eV)')
            ax.set_title('Frontier molecular orbital energy levels')
            ax.grid(axis='y',alpha=.15)
            fig.tight_layout(); fig.savefig(p,dpi=300,bbox_inches='tight'); plt.close(fig); made['orbitals']=p
    return made


def _report_fmt(v, digits=6, suffix=''):
    if v is None:
        return 'N/A'
    try:
        v = float(v)
        if abs(v) >= 1.0e5 or (abs(v) > 0 and abs(v) < 1.0e-4):
            s = f'{v:.6e}'
        else:
            s = f'{v:.{digits}f}'.rstrip('0').rstrip('.')
        return s + suffix
    except Exception:
        return str(v) + suffix


def _format_runtime(seconds):
    if seconds is None:
        return 'N/A'
    try:
        seconds = int(round(float(seconds)))
    except Exception:
        return str(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts=[]
    if d: parts.append(f'{d} d')
    if h or d: parts.append(f'{h} h')
    if m or h or d: parts.append(f'{m} min')
    parts.append(f'{s} s')
    return ' '.join(parts)


def _pdf_sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as fh:
        for block in iter(lambda: fh.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def build_pdf(a, plots, pdf_path):
    """Create the canonical ChemBot scientific report.

    This function is intentionally shared by Telegram-side .out analysis and
    the Kaggle runner. Keeping one implementation prevents the two report
    styles from drifting apart.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
        PageBreak, KeepTogether, LongTable, HRFlowable
    )

    NAVY = colors.HexColor('#17324D')
    SLATE = colors.HexColor('#4B5D6B')
    LIGHT = colors.HexColor('#F3F6F8')
    MID = colors.HexColor('#D9E1E7')
    GREEN = colors.HexColor('#1F7A4D')
    RED = colors.HexColor('#A33A32')
    TEXT = colors.HexColor('#202830')

    styles=getSampleStyleSheet()
    title_style=ParagraphStyle(
        'ReportTitle', parent=styles['Title'], fontName='Helvetica-Bold',
        fontSize=19, leading=22, textColor=NAVY, alignment=TA_LEFT,
        spaceAfter=4
    )
    subtitle_style=ParagraphStyle(
        'ReportSubtitle', parent=styles['BodyText'], fontName='Helvetica',
        fontSize=8.5, leading=11, textColor=SLATE, spaceAfter=8
    )
    h1=ParagraphStyle(
        'SectionH1', parent=styles['Heading2'], fontName='Helvetica-Bold',
        fontSize=12.2, leading=15, textColor=NAVY, spaceBefore=10, spaceAfter=5
    )
    h2=ParagraphStyle(
        'SectionH2', parent=styles['Heading3'], fontName='Helvetica-Bold',
        fontSize=9.8, leading=12, textColor=SLATE, spaceBefore=6, spaceAfter=4
    )
    body=ParagraphStyle(
        'ReportBody', parent=styles['BodyText'], fontName='Helvetica',
        fontSize=8.6, leading=11.3, textColor=TEXT
    )
    small=ParagraphStyle(
        'ReportSmall', parent=body, fontSize=7.3, leading=9.3, textColor=SLATE
    )
    caption=ParagraphStyle(
        'FigureCaption', parent=small, fontSize=7.4, leading=9.4,
        alignment=TA_CENTER, textColor=SLATE, spaceBefore=4, spaceAfter=7
    )
    mono=ParagraphStyle(
        'ReportMono', parent=body, fontName='Courier', fontSize=6.8, leading=8.4
    )

    filename=str(a.get('filename') or 'calculation.out')
    engine=str(a.get('engine') or 'Unknown')
    version=str(a.get('version') or 'N/A')
    method=str(a.get('method') or 'N/A')
    basis=str(a.get('basis') or 'N/A')
    normal=bool(a.get('normal_termination'))
    status='NORMAL TERMINATION' if normal else 'TERMINATION NOT CONFIRMED'

    doc=SimpleDocTemplate(
        pdf_path, pagesize=A4,
        rightMargin=1.45*cm, leftMargin=1.45*cm,
        topMargin=1.45*cm, bottomMargin=1.45*cm,
        title='Computational Chemistry Analysis Report',
        author='ChemBot'
    )

    def esc(x):
        return str(x if x is not None else '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    def cell(x, style=body):
        return Paragraph(esc(x), style)

    def section_title(label):
        return [Spacer(1,3), Paragraph(label, h1), HRFlowable(width='100%', thickness=0.6, color=MID, spaceAfter=5)]

    def styled_table(rows, widths=None, header=True, font_size=7.7):
        converted=[]
        for ridx,row in enumerate(rows):
            st = small if ridx or not header else ParagraphStyle(
                'TmpHdr', parent=small, fontName='Helvetica-Bold',
                textColor=colors.white, fontSize=7.5, leading=9
            )
            converted.append([cell(v, st) for v in row])
        t=LongTable(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign='LEFT')
        commands=[
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('LEFTPADDING',(0,0),(-1,-1),5),
            ('RIGHTPADDING',(0,0),(-1,-1),5),
            ('TOPPADDING',(0,0),(-1,-1),3.5),
            ('BOTTOMPADDING',(0,0),(-1,-1),3.5),
            ('GRID',(0,0),(-1,-1),0.25,MID),
            ('FONTNAME',(0,0),(-1,-1),'Helvetica'),
            ('FONTSIZE',(0,0),(-1,-1),font_size),
        ]
        if header:
            commands += [
                ('BACKGROUND',(0,0),(-1,0),NAVY),
                ('TEXTCOLOR',(0,0),(-1,0),colors.white),
                ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ]
            first_data=1
        else:
            first_data=0
        for r in range(first_data, len(rows)):
            if (r-first_data) % 2:
                commands.append(('BACKGROUND',(0,r),(-1,r),LIGHT))
        t.setStyle(TableStyle(commands))
        return t

    def metric_rows():
        e=a.get('energies',{}) or {}
        t=a.get('thermochemistry',{}) or {}
        o=a.get('orbitals',{}) or {}
        d=a.get('dipole',{}) or {}
        timing=a.get('timings',{}) or {}
        vals=[
            ('Final electronic energy', _report_fmt(e.get('final_energy_hartree'), 10, ' Eh')),
            ('Gibbs free energy', _report_fmt(t.get('gibbs_hartree'), 10, ' Eh')),
            ('HOMO', _report_fmt(o.get('homo_ev'), 4, ' eV')),
            ('LUMO', _report_fmt(o.get('lumo_ev'), 4, ' eV')),
            ('HOMO-LUMO gap', _report_fmt(o.get('gap_ev'), 4, ' eV')),
            ('Dipole magnitude', _report_fmt(d.get('magnitude_debye'), 4, ' D')),
            ('Runtime', _format_runtime(timing.get('wall_seconds'))),
        ]
        return [(k,v) for k,v in vals if not v.startswith('N/A')]

    def add_figure(story, key, label, note=None, max_h=10.3*cm):
        path=plots.get(key)
        if not path or not os.path.exists(path):
            return
        im=Image(path)
        max_w=17.0*cm
        scale=min(max_w/float(im.imageWidth), max_h/float(im.imageHeight))
        im.drawWidth=im.imageWidth*scale
        im.drawHeight=im.imageHeight*scale
        story.append(KeepTogether([
            Spacer(1,5), im,
            Paragraph(label + (f' {note}' if note else ''), caption)
        ]))

    def page_frame(canvas, doc_obj):
        canvas.saveState()
        w,h=A4
        canvas.setStrokeColor(MID)
        canvas.setLineWidth(0.4)
        canvas.line(doc_obj.leftMargin, h-0.78*cm, w-doc_obj.rightMargin, h-0.78*cm)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(SLATE)
        canvas.drawString(doc_obj.leftMargin, h-0.60*cm, 'ChemBot computational chemistry analysis')
        right=f'{engine} {version}'
        canvas.drawRightString(w-doc_obj.rightMargin, h-0.60*cm, right[:55])
        canvas.line(doc_obj.leftMargin, 0.72*cm, w-doc_obj.rightMargin, 0.72*cm)
        canvas.drawString(doc_obj.leftMargin, 0.45*cm, filename[:68])
        canvas.drawRightString(w-doc_obj.rightMargin, 0.45*cm, f'Page {doc_obj.page}')
        canvas.restoreState()

    story=[]
    story += [
        Paragraph('Computational Chemistry Analysis Report', title_style),
        Paragraph(
            f'<b>File:</b> {esc(filename)} &nbsp;&nbsp; | &nbsp;&nbsp; '
            f'<b>Report engine:</b> ChemBot {REPORT_GENERATOR_VERSION}',
            subtitle_style
        ),
    ]

    status_color = GREEN if normal else RED
    status_tab=Table(
        [[Paragraph(f'<b>{status}</b>', ParagraphStyle(
            'StatusText', parent=body, fontName='Helvetica-Bold',
            fontSize=9.2, textColor=colors.white, alignment=TA_CENTER
        ))]],
        colWidths=[17.0*cm], rowHeights=[0.70*cm]
    )
    status_tab.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),status_color),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('BOX',(0,0),(-1,-1),0,status_color),
    ]))
    story += [status_tab, Spacer(1,8)]

    sysinfo=a.get('system',{}) or {}
    meta_rows=[
        ['Engine', engine, 'Version', version],
        ['Method', method, 'Basis', basis],
        ['Charge', sysinfo.get('charge','N/A'), 'Multiplicity', sysinfo.get('multiplicity','N/A')],
        ['Atoms', len(a.get('final_geometry',[]) or []) or 'N/A', 'Runtime', _format_runtime((a.get('timings',{}) or {}).get('wall_seconds'))],
    ]
    mt=Table([[cell(x, body) for x in r] for r in meta_rows],
             colWidths=[2.5*cm,6.0*cm,2.5*cm,6.0*cm])
    mt.setStyle(TableStyle([
        ('GRID',(0,0),(-1,-1),0.25,MID),
        ('BACKGROUND',(0,0),(0,-1),LIGHT),
        ('BACKGROUND',(2,0),(2,-1),LIGHT),
        ('FONTNAME',(0,0),(-1,-1),'Helvetica'),
        ('FONTNAME',(0,0),(0,-1),'Helvetica-Bold'),
        ('FONTNAME',(2,0),(2,-1),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,-1),8),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('LEFTPADDING',(0,0),(-1,-1),5),
        ('TOPPADDING',(0,0),(-1,-1),4),
        ('BOTTOMPADDING',(0,0),(-1,-1),4),
    ]))
    story += [mt]

    story += section_title('Key results')
    metrics=metric_rows()
    if metrics:
        rows=[['Quantity','Value']] + [[k,v] for k,v in metrics]
        story += [styled_table(rows,[8.5*cm,8.5*cm]), Spacer(1,5)]

    # Calculation quality / frequency diagnostic
    freqs=[float(x) for x in (a.get('frequencies_cm1',[]) or []) if x is not None]
    imag_strict=[x for x in freqs if x < -5.0]
    near_zero=[x for x in freqs if abs(x) < 5.0]
    positive=[x for x in freqs if x >= 5.0]
    if freqs:
        diag_rows=[
            ['Frequency diagnostic','Value'],
            ['Normal-mode entries parsed', str(len(freqs))],
            ['Near-zero modes (|nu| < 5 cm-1)', str(len(near_zero))],
            ['Imaginary modes (nu < -5 cm-1)', str(len(imag_strict))],
            ['Positive modes (nu >= 5 cm-1)', str(len(positive))],
        ]
        story += [Paragraph(
            'Frequency counts are reported separately so translational/rotational near-zero entries are not mislabeled as vibrational modes.',
            small
        ), Spacer(1,4), styled_table(diag_rows,[11.5*cm,5.5*cm])]

    # Energies and thermochemistry
    story += section_title('Energetics and thermochemistry')
    e=a.get('energies',{}) or {}
    t=a.get('thermochemistry',{}) or {}
    energy_rows=[['Quantity','Value','Unit']]
    if e.get('final_energy_hartree') is not None:
        energy_rows.append(['Final electronic energy', _report_fmt(e['final_energy_hartree'],10), 'Eh'])
    thermo_map=[
        ('temperature_K','Temperature','K',4),
        ('pressure_atm','Pressure','atm',4),
        ('zpe_hartree','Zero-point energy','Eh',8),
        ('thermal_energy_correction_hartree','Thermal energy correction','Eh',8),
        ('enthalpy_correction_hartree','Enthalpy correction','Eh',8),
        ('gibbs_correction_hartree','Gibbs correction','Eh',8),
        ('enthalpy_hartree','Total enthalpy','Eh',10),
        ('gibbs_hartree','Gibbs free energy','Eh',10),
        ('entropy_J_mol_K','Entropy','J mol-1 K-1',5),
    ]
    for key,label,unit,dig in thermo_map:
        if t.get(key) is not None:
            energy_rows.append([label,_report_fmt(t[key],dig),unit])
    if len(energy_rows)>1:
        story += [styled_table(energy_rows,[9.0*cm,4.5*cm,3.5*cm])]
    add_figure(story,'optimization','Figure: optimization energy profile. Energies are shown relative to the minimum parsed optimization energy.')

    # Vibrational analysis
    if freqs or a.get('ir_spectrum') or a.get('raman_spectrum'):
        story += section_title('Vibrational spectroscopy')
        vib_rows=[['Metric','Value']]
        if freqs:
            vib_rows += [
                ['Normal-mode entries parsed', str(len(freqs))],
                ['Near-zero modes (|nu| < 5 cm-1)', str(len(near_zero))],
                ['Imaginary modes (nu < -5 cm-1)', str(len(imag_strict))],
                ['Lowest non-zero mode', _report_fmt(min(positive) if positive else None,2,' cm-1')],
                ['Highest mode', _report_fmt(max(freqs) if freqs else None,2,' cm-1')],
            ]
        story += [styled_table(vib_rows,[11.0*cm,6.0*cm])]
        if imag_strict:
            story += [Spacer(1,4), Paragraph(
                '<b>Imaginary frequencies:</b> ' + ', '.join(f'{x:.2f} cm-1' for x in imag_strict[:20]),
                small
            )]

        ir=a.get('ir_spectrum',[]) or []
        ir_clean=[]
        for pair in ir:
            try:
                f=float(pair[0]); inten=abs(float(pair[1] or 0.0))
            except Exception:
                continue
            if f>0:
                ir_clean.append((f,inten))
        if ir_clean:
            ranked=sorted(ir_clean,key=lambda q:q[1],reverse=True)[:12]
            mx=max([x[1] for x in ir_clean] or [1.0])
            peak_rows=[['Frequency (cm-1)','Relative intensity (%)']]
            for f,inten in ranked:
                peak_rows.append([f'{f:.2f}', f'{100.0*inten/mx:.1f}' if mx>0 else '0.0'])
            story += [Spacer(1,6), Paragraph('Strongest calculated IR bands',h2),
                      styled_table(peak_rows,[8.5*cm,8.5*cm])]
        add_figure(
            story,'ir',
            'Figure: simulated FT-IR-like profile.',
            'The y-axis is a normalized transmittance-like visualization derived from calculated intensities; it is not experimental %T.'
        )
        add_figure(story,'raman','Figure: calculated Raman spectrum.')

    # TD-DFT
    td=a.get('tddft_states',[]) or []
    if td:
        story += section_title('Electronic excitations / TD-DFT')
        rows=[['State','Energy (eV)','Wavelength (nm)','Oscillator strength']]
        for s in td[:30]:
            rows.append([
                s.get('state',''),
                _report_fmt(s.get('ev'),4),
                _report_fmt(s.get('nm'),2),
                _report_fmt(s.get('f'),6),
            ])
        story += [styled_table(rows,[2.2*cm,4.2*cm,4.5*cm,6.1*cm])]
        if len(td)>30:
            story += [Paragraph(f'First 30 of {len(td)} parsed excited states are shown.', small)]
        add_figure(story,'uvvis','Figure: simulated UV-Vis spectrum from parsed TD-DFT transitions.')

    # Orbitals
    orb=a.get('orbitals',{}) or {}
    arr=orb.get('orbitals',[]) or []
    if arr or orb.get('homo_ev') is not None:
        story += section_title('Frontier orbital energies')
        fr=[['Quantity','Energy (eV)']]
        if orb.get('homo_ev') is not None: fr.append(['HOMO',_report_fmt(orb.get('homo_ev'),5)])
        if orb.get('lumo_ev') is not None: fr.append(['LUMO',_report_fmt(orb.get('lumo_ev'),5)])
        if orb.get('gap_ev') is not None: fr.append(['HOMO-LUMO orbital-energy gap',_report_fmt(orb.get('gap_ev'),5)])
        story += [styled_table(fr,[11.0*cm,6.0*cm])]
        desc=a.get('conceptual_dft',{}) or {}
        if desc:
            drows=[['Conceptual-DFT descriptor','Value']]
            descriptor_labels=[
                ('ionization_potential_ev','Ionization potential, I',' eV'),
                ('electron_affinity_ev','Electron affinity, A',' eV'),
                ('chemical_hardness_ev','Chemical hardness, eta',' eV'),
                ('chemical_potential_ev','Chemical potential, mu',' eV'),
                ('electronegativity_ev','Electronegativity, chi',' eV'),
                ('chemical_softness_ev','Chemical softness, S',' eV^-1'),
                ('electrophilicity_index_ev','Electrophilicity index, omega',' eV'),
                ('electrodonating_power_ev','Electrodonating power, omega-',' eV'),
                ('electroaccepting_power_ev','Electroaccepting power, omega+',' eV'),
                ('net_electrophilicity_ev','Net electrophilicity, Delta omega',' eV'),
            ]
            for key,label,suffix in descriptor_labels:
                if desc.get(key) is not None:
                    drows.append([label,_report_fmt(desc.get(key),6,suffix)])
            if len(drows)>1:
                story += [Spacer(1,6),Paragraph('Conceptual DFT descriptors',h2),styled_table(drows,[11.0*cm,6.0*cm]),
                          Paragraph('Frontier-orbital approximations using the same Koopmans/Parr-Pearson definitions implemented by ORCA_ENGINE.',small)]
        if arr:
            frontier=_frontier_orbital_records_v62(orb,per_side=6)
            rows=[['Spin','Label','Index','Occ.','Energy (Eh)','Energy (eV)']]
            for spin,label,z in frontier:
                rows.append([spin,label,z.get('index',''),_report_fmt(z.get('occ'),2),_report_fmt(z.get('eh'),7),_report_fmt(z.get('ev'),5)])
            story += [Spacer(1,6), Paragraph('Frontier orbital window',h2),
                      styled_table(rows,[2.4*cm,2.6*cm,1.5*cm,1.5*cm,4.5*cm,4.5*cm])]
        add_figure(
            story,'orbitals',
            'Figure: frontier orbital energy-level diagram.',
            'This is an energy diagram from printed orbital data, not a 3D orbital isosurface; the gap is not an excitation energy.'
        )

    # Molecular properties
    d=a.get('dipole',{}) or {}
    charges=a.get('atomic_charges',[]) or []
    geom=a.get('final_geometry',[]) or []
    if d or charges or geom:
        story += section_title('Molecular properties')
        prop=[['Property','Value']]
        if sysinfo.get('charge') is not None: prop.append(['Total charge',str(sysinfo.get('charge'))])
        if sysinfo.get('multiplicity') is not None: prop.append(['Multiplicity',str(sysinfo.get('multiplicity'))])
        if d.get('magnitude_debye') is not None: prop.append(['Dipole magnitude',_report_fmt(d.get('magnitude_debye'),5,' D')])
        if geom: prop.append(['Atoms in final Cartesian geometry',str(len(geom))])
        if charges: prop.append(['Atomic charges parsed',str(len(charges))])
        story += [styled_table(prop,[10.5*cm,6.5*cm])]

    # Diagnostics
    story += section_title('Diagnostics')
    errs=a.get('errors',[]) or []
    diag=[
        ['Check','Result'],
        ['Normal termination detected','Yes' if normal else 'No'],
        ['Process return code',str(a.get('process_returncode','N/A'))],
        ['Parsed warning/error lines',str(len(errs))],
        ['Report generator',REPORT_GENERATOR_VERSION],
    ]
    story += [styled_table(diag,[11.5*cm,5.5*cm])]
    if errs:
        story += [Spacer(1,5), Paragraph('Last detected diagnostic lines',h2)]
        for line in errs[-12:]:
            story.append(Paragraph(esc(line), mono))

    # Appendices for full machine-readable details
    if charges or geom or arr:
        story.append(PageBreak())
        story += section_title('Appendix - detailed numerical data')

    if charges:
        story += [Paragraph('Atomic charges',h2)]
        rows=[['Index','Atom','Charge']]
        for x in charges:
            rows.append([x.get('index',''),x.get('element',''),_report_fmt(x.get('charge'),7)])
        story += [styled_table(rows,[3.0*cm,4.0*cm,10.0*cm])]

    if geom:
        story += [Spacer(1,8), Paragraph('Final Cartesian geometry (Angstrom)',h2)]
        rows=[['Atom','X','Y','Z']]
        for x in geom:
            rows.append([
                x.get('element',''),
                _report_fmt(x.get('x'),7),
                _report_fmt(x.get('y'),7),
                _report_fmt(x.get('z'),7),
            ])
        story += [styled_table(rows,[3.0*cm,4.65*cm,4.65*cm,4.65*cm])]

    if arr:
        story += [Spacer(1,8), Paragraph('Orbital energies - frontier-centered extract',h2)]
        occ=[z for z in arr if (z.get('occ') or 0)>1e-8]
        vir=[z for z in arr if (z.get('occ') or 0)<=1e-8]
        appendix_arr=(occ[-20:] if occ else []) + (vir[:20] if vir else [])
        rows=[['Index','Spin','Occ.','Energy (Eh)','Energy (eV)']]
        for z in appendix_arr:
            rows.append([
                z.get('index',''), z.get('spin','restricted'),
                _report_fmt(z.get('occ'),3), _report_fmt(z.get('eh'),8), _report_fmt(z.get('ev'),6)
            ])
        story += [styled_table(rows,[2.0*cm,3.5*cm,2.0*cm,4.8*cm,4.8*cm])]
        if len(arr)>len(appendix_arr):
            story += [Paragraph(
                f'This appendix shows {len(appendix_arr)} frontier-centered orbitals out of {len(arr)} parsed orbitals. '
                'The complete parsed dataset is preserved in analysis.json.',
                small
            )]

    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return pdf_path


def generate_report_bundle(a, outdir, base_name=None):
    """Canonical report bundle used identically by local Telegram analysis and Kaggle."""
    os.makedirs(outdir, exist_ok=True)
    base=(base_name or Path(str(a.get('filename') or 'calculation')).stem).strip() or 'calculation'
    plots=make_plots(a,outdir)
    pdf_path=os.path.join(outdir,base+'_analysis_report.pdf')
    build_pdf(a,plots,pdf_path)

    enriched=dict(a)
    enriched['report_generator_version']=REPORT_GENERATOR_VERSION
    analysis_path=os.path.join(outdir,'analysis.json')
    with open(analysis_path,'w',encoding='utf-8') as fh:
        json.dump(enriched,fh,indent=2,ensure_ascii=False)

    manifest={
        'report_generator_version': REPORT_GENERATOR_VERSION,
        'filename': a.get('filename'),
        'engine': a.get('engine'),
        'pdf': os.path.basename(pdf_path),
        'pdf_sha256': _pdf_sha256(pdf_path),
        'plots': {k: os.path.basename(v) for k,v in plots.items() if v and os.path.exists(v)},
    }
    manifest_path=os.path.join(outdir,'report_manifest.json')
    with open(manifest_path,'w',encoding='utf-8') as fh:
        json.dump(manifest,fh,indent=2,ensure_ascii=False)

    return {
        'plots':plots,
        'pdf':pdf_path,
        'analysis_json':analysis_path,
        'manifest':manifest_path,
        'version':REPORT_GENERATOR_VERSION,
    }

'''

# ============================================================
# v4.1 scientific parser hardening
# ORCA patterns align to chemistry-web-lab/orca_engine RegexLibrary.
# Psi4 parsing is independent and follows native Psi4 output tables.
# ============================================================
ANALYZER_V41_PATCH_CODE = r'''
FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"

def detect_engine(text):
    """Identify ORCA vs Psi4 from multiple independent output signatures.

    A single banner is not required: partially written ORCA files commonly
    lack the final termination line, while cropped outputs may omit the ASCII
    logo.  Weighted signatures avoid routing an ORCA output through the Psi4
    parser (or the generic fallback) merely because one banner is missing.
    """
    u = (text or '').upper()
    if not u.strip():
        return 'Unknown'

    orca_markers = (
        ('O   R   C   A', 10),
        ('ORCA TERMINATED NORMALLY', 10),
        ('ORCA TERMINATED ABNORMALLY', 10),
        ('ORCA PROPERTY CALCULATIONS', 7),
        ('FINAL SINGLE POINT ENERGY', 7),
        ('ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS', 7),
        ('YOUR CALCULATION UTILIZES THE BASIS:', 6),
        ('CARTESIAN COORDINATES (ANGSTROEM)', 5),
        ('CARTESIAN COORDINATES (A.U.)', 4),
        ('DFT DISPERSION CORRECTION', 4),
        ('MULLIKEN ATOMIC CHARGES', 3),
        ('HIRSHFELD ANALYSIS', 3),
        ('VIBRATIONAL FREQUENCIES', 2),
        ('PROGRAM VERSION', 1),
    )
    psi4_markers = (
        ('PSI4 EXITING SUCCESSFULLY', 10),
        ('AN OPEN-SOURCE AB INITIO ELECTRONIC STRUCTURE PACKAGE', 10),
        ('PSI4', 7),
        ('PSIEXCEPTION', 7),
        ('@RKS FINAL ENERGY:', 6),
        ('@UKS FINAL ENERGY:', 6),
        ('@RHF FINAL ENERGY:', 6),
        ('@UHF FINAL ENERGY:', 6),
        ('@DF-RKS FINAL ENERGY:', 6),
        ('@DF-RHF FINAL ENERGY:', 6),
        ('IR ACTIV [KM/MOL]', 4),
        ('GIBBS FREE ENERGY', 2),
    )

    orca_score = sum(weight for marker, weight in orca_markers if marker in u)
    psi4_score = sum(weight for marker, weight in psi4_markers if marker in u)

    # Strong explicit identities always win over incidental shared wording.
    if ('O   R   C   A' in u or 'ORCA TERMINATED' in u) and 'PSI4 EXITING SUCCESSFULLY' not in u:
        return 'ORCA'
    if ('PSI4 EXITING SUCCESSFULLY' in u or 'AN OPEN-SOURCE AB INITIO ELECTRONIC STRUCTURE PACKAGE' in u) and 'ORCA TERMINATED' not in u:
        return 'Psi4'

    if orca_score >= 5 and orca_score > psi4_score:
        return 'ORCA'
    if psi4_score >= 5 and psi4_score > orca_score:
        return 'Psi4'

    # A few ORCA records are sufficiently characteristic even in truncated
    # files and are safer than returning Unknown.
    if 'FINAL SINGLE POINT ENERGY' in u or 'YOUR CALCULATION UTILIZES THE BASIS:' in u:
        return 'ORCA'
    return 'Unknown'

def _nums(line):
    return [_float(x) for x in re.findall(FLOAT_RE, line)]

def _last_match_value(text, pattern, group='value', flags=re.I | re.M):
    ms=list(re.finditer(pattern,text,flags))
    if not ms: return None
    try: return _float(ms[-1].group(group))
    except Exception: return _float(ms[-1].group(1))

def _orca_input_method(text):
    """Recover the user-requested ORCA method from the echoed SimpleInput line.

    ORCA's descriptive Exchange/Correlation lines may report component
    functionals (e.g. PBE/PBE) even when the actual hybrid method is PBE0.
    The echoed input keyword is therefore authoritative when available.
    """
    candidates = []
    for line in text.splitlines():
        m = re.match(r'^\s*\|\s*\d+>\s*!\s*(.+)$', line)
        if m:
            candidates.append(m.group(1))
        else:
            m = re.match(r'^\s*!\s+(.+)$', line)
            if m:
                candidates.append(m.group(1))

    # Most specific patterns first. Canonical spelling is preserved.
    method_patterns = [
        (r'\bDLPNO-CCSD\(T\)\b', 'DLPNO-CCSD(T)'),
        (r'\bDLPNO-CCSD\b', 'DLPNO-CCSD'),
        (r'\bCCSD\(T\)\b', 'CCSD(T)'),
        (r'\bCCSD\b', 'CCSD'),
        (r'\bNEVPT2\b', 'NEVPT2'),
        (r'\bCASSCF\b', 'CASSCF'),
        (r'\bRI-?MP2\b', 'RI-MP2'),
        (r'\bMP2\b', 'MP2'),
        (r'\bCAM-?B3LYP\b', 'CAM-B3LYP'),
        (r'\bB3LYP\b', 'B3LYP'),
        (r'\bPBE0\b', 'PBE0'),
        (r'\bM06-?2X\b', 'M06-2X'),
        (r'\bM06-?L\b', 'M06-L'),
        (r'\bM06\b', 'M06'),
        (r'\b(?:w|ω)B97X-?D4\b', 'wB97X-D4'),
        (r'\b(?:w|ω)B97X\b', 'wB97X'),
        (r'\b(?:w|ω)B97M-?V\b', 'wB97M-V'),
        (r'\br2SCAN-?3C\b', 'r2SCAN-3C'),
        (r'\bB97-?3C\b', 'B97-3C'),
        (r'\bTPSSh\b', 'TPSSh'),
        (r'\bTPSS\b', 'TPSS'),
        (r'\bBP86\b', 'BP86'),
        (r'\bBLYP\b', 'BLYP'),
        (r'\brevPBE\b', 'revPBE'),
        (r'\bPBE\b', 'PBE'),
        (r'\bHF\b', 'HF'),
    ]
    for line in candidates:
        for rx, canonical in method_patterns:
            if re.search(rx, line, re.I):
                return canonical

    # Fallback: search only the early input/header region, not the whole
    # calculation tables, to avoid misidentifying component functional names.
    head = '\n'.join(text.splitlines()[:500])
    for rx, canonical in method_patterns:
        if re.search(rx, head, re.I):
            return canonical
    return None


def _orca_split_jobs(text):
    """Split ORCA output into logical job blocks using ORCA_ENGINE semantics."""
    jobs, cur = [], []
    seen = False
    for line in text.splitlines():
        stripped = line.strip()
        starts = ('$new_job' in stripped.lower()) or ('O   R   C   A' in stripped and seen)
        if starts and cur:
            jobs.append('\n'.join(cur))
            cur = []
            seen = False
        cur.append(line)
        if stripped and (
            'FINAL SINGLE POINT ENERGY' in stripped.upper()
            or 'Program Version' in stripped
            or 'CARTESIAN COORDINATES' in stripped.upper()
            or 'ORCA TERMINATED' in stripped.upper()
        ):
            seen = True
    if cur:
        jobs.append('\n'.join(cur))
    return [j for j in jobs if j.strip()]


def _orca_parse_one_job(text, filename):
    """ORCA parser compatible with chemistry-web-lab/orca_engine state machine.

    The section reset rules, table anchors, thermochemistry labels, TD-DFT
    absorption table, coordinate-unit handling, charge tables, and stationary
    point classification mirror ORCA_ENGINE. The output is adapted only to the
    Telegram bot's historical dictionary schema.
    """
    F = FLOAT_RE
    lines = text.splitlines()

    # ---------- scalar/global observables ----------
    normal = None
    errors = []
    version = None
    basis = None
    method = None
    exchange = None
    correlation = None
    dispersion = None
    solvation = None
    solvent = None
    charge = None
    multiplicity = None
    dipole = None
    s2_actual = None
    s2_ideal = None

    thermo = {}
    final_energy = None
    optimization_energies = []

    # ---------- stateful/final-section observables ----------
    state = 'SEARCHING'
    coord_unit = None
    final_geometry = []
    current_geometry = []
    orbitals_all = []
    current_orbitals = []
    current_spin = 'restricted'
    td_cm = []
    td_fosc = []
    freqs = []
    imag = []
    ir = []
    ir_has_epsilon = None
    charge_type = None
    atomic_charge_sets = {}
    expecting_basis = False

    # Regexes copied/aligned to ORCA_ENGINE RegexLibrary.
    rx_version = re.compile(r'Program Version\s+(?P<version>\d+(?:\.\d+)*)', re.I)
    rx_basis = re.compile(r'Your calculation utilizes the basis:\s*(?P<basis>.*)$', re.I)
    rx_exchange = re.compile(r'Exchange Functional\s+Exchange\s*\.+\s*(?P<value>\S+)', re.I)
    rx_corr = re.compile(r'Correlation Functional\s+Correlation\s*\.+\s*(?P<value>\S+)', re.I)
    rx_disp = re.compile(r'\bDFT\s+DISPERSION\s+CORRECTION\b|(?:vdW-correction\s*:|Dispersion\s+correction\s*\.+)\s*(?P<value>D3BJ|D4|DFT-D3)', re.I)
    rx_solvent = re.compile(r'Solvent:\s*(?P<solvent>\S+)', re.I)
    rx_cpcm = re.compile(r'CPCM\s+Solvation\s+Model\s+.*\s+Solvent\s*:\s*(?P<solvent>\S+)', re.I)
    rx_smd = re.compile(r'SMD\s+(?:Solvation\s+Model\s+.*\s+Solvent\s*:|solvent\s*\.+)\s*(?P<solvent>\S+)', re.I)
    rx_solvation = re.compile(r'utilizes the\s+(?P<model>\w+)\s+solvation module', re.I)
    rx_charge = re.compile(r'Total Charge\s+Charge\s*\.+\s*(?P<value>[-+]?\d+)', re.I)
    rx_mult = re.compile(r'Multiplicity\s+Mult\s*\.+\s*(?P<value>\d+)', re.I)
    rx_temp = re.compile(rf'^\s*Temperature\s*\.+\s*(?P<value>{F})\s*K\b', re.I)
    rx_press = re.compile(rf'^\s*Pressure\s*\.+\s*(?P<value>{F})\s*atm\b', re.I)
    rx_sp = re.compile(rf'FINAL SINGLE POINT ENERGY\s+(?P<value>{F})', re.I)
    rx_zpe = re.compile(rf'(?:(?:Non-thermal|Total)\s+)?Zero[ -]point (?:vibrational )?energy\s*\.*\s*(?P<value>{F})\s*Eh', re.I)
    rx_thermal_e = re.compile(rf'Total thermal energy\s*\.+\s*(?P<value>{F})\s*Eh', re.I)
    rx_thermal_corr = re.compile(rf'Thermal energy correction\s*\.+\s*(?P<value>{F})\s*Eh', re.I)
    rx_hcorr = re.compile(rf'Thermal\s+(?:Enthalpy|[Ee]nthalpy)\s+correction\s*\.+\s*(?P<value>{F})\s*Eh', re.I)
    rx_gcorr = re.compile(rf'Thermal(?: Gibbs)?\s+(?:free\s+)?[Ee]nergy correction\s*\.+\s*(?P<value>{F})\s*Eh', re.I)
    rx_g = re.compile(rf'(?:Final|Total)\s+Gibbs\s+(?:free\s+)?(?:energy|enthalpy)\s*\.*\s*(?P<value>{F})\s*Eh', re.I)
    rx_h = re.compile(rf'(?:Total|Final)(?:\s+thermal)?\s+enthalpy\s*\.*\s*(?P<value>{F})\s*Eh', re.I)
    rx_entropy_term = re.compile(rf'Final entropy term\s*\.*\s*(?P<value>{F})\s*Eh', re.I)
    rx_entropy_corr = re.compile(rf'Total entropy correction\s*\.*\s*(?P<value>{F})\s*Eh', re.I)
    rx_svib = re.compile(rf'S_vib\s*\.+\s*(?P<value>{F})\s*cal/mol-K', re.I)
    rx_srot = re.compile(rf'S_rot\s*\.+\s*(?P<value>{F})\s*cal/mol-K', re.I)
    rx_strans = re.compile(rf'S_trans\s*\.+\s*(?P<value>{F})\s*cal/mol-K', re.I)
    rx_selec = re.compile(rf'S_elec\s*\.+\s*(?P<value>{F})\s*cal/mol-K', re.I)
    rx_qrrho = re.compile(r'(?P<treatment>quasi[ \-]?RRHO\s+method\s+of\s+Grimme|Grimme[ \-]?quasi[ \-]?RRHO|Standard-RRHO|modified\s+free\s+rotor)', re.I)
    rx_qcut = re.compile(rf'Frequency cutoff for the RRHO\s*\.+\s*(?P<value>{F})\s*cm\*\*-1', re.I)
    rx_dipole = re.compile(rf'(?:Total\s+Dipole\s+Moment\s*:\s*|Magnitude\s*\(\s*Debye\s*\)\s*:\s*)(?P<value>{F})', re.I)
    rx_s2 = re.compile(rf'(?<!Ideal\s)<\s*S\*\*2\s*>\s*:\s*(?P<value>{F})', re.I)
    rx_s2i = re.compile(rf'Ideal\s*<\s*S\*\*2\s*>\s*:\s*(?P<value>{F})', re.I)
    rx_normal = re.compile(r'ORCA\s+TERMINATED\s+NORMALLY', re.I)
    rx_fatal = re.compile(r'(?:ORCA\s+finished\s+by\s+error\s+termination|\bORCA\s+TERMINATED\s+ABNORMALLY\b|\bTERMINATED\s+ABNORMALLY\b|\bAn\s+error\s+has\s+occurred\b|\bINPUT\s+ERROR\b|\bABORTING\s+THE\s+RUN\b|\bUNRECOGNIZED\s+OR\s+DUPLICATED\s+KEYWORD)', re.I)

    rx_coord = re.compile(r'\bCARTESIAN\s+COORDINATES\b', re.I)
    rx_coord_ang = re.compile(r'\bCARTESIAN\s+COORDINATES\s*\(\s*ANGSTROEM\s*\)', re.I)
    rx_coord_au = re.compile(r'\bCARTESIAN\s+COORDINATES\s*\(\s*A\.U\.\s*\)', re.I)
    rx_orbsec = re.compile(r'\b(?:ORBITAL\s+ENERGIES|MOLECULAR\s+ORBITALS|MO\s+ENERGIES|MOLECULAR\s+ORBITAL\s+ENERGIES)\b', re.I)
    rx_spin_up = re.compile(r'\b(?:SPIN\s+UP\s+ORBITALS|ALPHA\s+(?:MOLECULAR\s+)?ORBITALS)\b', re.I)
    rx_spin_dn = re.compile(r'\b(?:SPIN\s+DOWN\s+ORBITALS|BETA\s+(?:MOLECULAR\s+)?ORBITALS)\b', re.I)
    rx_orbrow = re.compile(rf'^\s*(?P<idx>\d+)\s+(?P<occ>{F})\s+(?P<eh>{F})(?:\s+(?P<ev>{F}))?(?:\s|$)', re.I)

    rx_tdsec = re.compile(r'(?<!CORRECTED\s)\bABSORPTION\s+SPECTRUM\s+VIA\s+TRANSITION\s+ELECTRIC\s+DIPOLE\s+MOMENTS\b', re.I)
    rx_tdsoc = re.compile(r'\bSOC\s+CORRECTED\s+ABSORPTION\s+SPECTRUM\s+VIA\s+TRANSITION\s+ELECTRIC\s+DIPOLE\s+MOMENTS\b', re.I)
    rx_td = re.compile(rf'^\s*\d+\s+(?P<cm>{F})\s+(?P<nm>{F})\s+(?P<fosc>{F})(?:\s|$)', re.I)
    rx_tdtrans = re.compile(rf'^\s*\S+\s+->\s+\S+\s+(?P<ev>{F})\s+(?P<cm>{F})\s+(?P<nm>{F})\s+(?P<fosc>{F})(?:\s|$)', re.I)

    rx_freqsec = re.compile(r'\b(?:VIBRATIONAL\s+FREQUENCIES|3N-6\s+VIBRATIONAL\s+FREQUENCIES|3N-5\s+VIBRATIONAL\s+FREQUENCIES)\b', re.I)
    rx_freqrow = re.compile(rf'^\s*\d+:\s*(?P<cm>{F})(?:\s*cm\*\*-1|\s*cm\^-1|\s*cm-1)?(?P<imag>\s*(?:\*\*\*imaginary\s+mode\*\*\*|\(imaginary\s+mode\)|imaginary))?', re.I)
    rx_irsec = re.compile(r'\bIR\s+SPECTRUM\b', re.I)
    rx_irrow = re.compile(rf'^\s*(?P<mode>\d+):\s*(?P<freq>{F})\s+(?P<first>{F})(?:\s+(?P<second>{F}))?(?:\s+(?P<third>{F}))?(?=\s|$)', re.I)

    rx_hirsh = re.compile(r'\bHIRSHFELD\s+(?:ANALYSIS|CHARGES|POPULATION\s+ANALYSIS)\b', re.I)
    rx_mull = re.compile(r'\bMULLIKEN\s+(?:ATOMIC\s+)?(?:CHARGES|POPULATION\s+ANALYSIS)\b', re.I)
    rx_loew = re.compile(r'\bL[OÖ]EWDIN\s+(?:ATOMIC\s+)?(?:CHARGES|POPULATION\s+ANALYSIS)\b', re.I)
    rx_chelpg = re.compile(r'\bCHELPG\s+(?:CHARGES|POPULATION\s+ANALYSIS)\b', re.I)
    rx_mayer = re.compile(r'\bMAYER\s+POPULATION(?:\s+ANALYSIS)?\b', re.I)
    rx_chg_colon = re.compile(rf'^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}})\s*:\s*(?P<charge>{F})', re.I)
    rx_chg_table = re.compile(rf'^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}})\s+(?P<charge>{F})(?:\s+(?P<spin>{F}))?', re.I)
    rx_mayerrow = re.compile(rf'^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}}):?\s+(?P<na>{F})\s+(?P<za>{F})\s+(?P<qa>{F})(?:\s+(?P<va>{F}))?', re.I)

    def table_noise(line):
        s = line.strip()
        u = s.upper()
        return (bool(s) and set(s) <= {'-'}) or u.startswith(('NO ', 'STATE')) or 'E(EH)' in u or 'E(EV)' in u or 'CM**-1' in u

    def major(line):
        u = line.strip().upper()
        return ('FINAL SINGLE POINT ENERGY' in u or 'TOTAL RUN TIME' in u or 'ORCA TERMINATED' in u
                or u.startswith(('=> NOW LEAVING', 'CIS/TD-DFT', 'ORCA PROPERTY CALCULATIONS')))

    def coord_from_line(line):
        parts = line.split()
        if len(parts) < 4:
            return None
        try:
            x, y, z = map(float, parts[-3:])
        except Exception:
            return None
        for tok in parts[:-3]:
            m = re.fullmatch(r'(?P<sym>[A-Za-z]{1,3})(?P<ghost>:?)', tok)
            if m:
                sym = m.group('sym').capitalize() + (':' if m.group('ghost') else '')
                return {'element':sym, 'x':x, 'y':y, 'z':z}
        return None

    # SimpleInput recovery is authoritative for hybrid names such as PBE0.
    input_method = _orca_input_method(text)

    # Track a next-line basis label like ORCA_ENGINE.
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # ---------- global dispatch ----------
        m = rx_version.search(stripped)
        if m: version = m.group('version')

        for key, rx in (
            ('thermal_energy_hartree', rx_thermal_e),
            ('thermal_energy_correction_hartree', rx_thermal_corr),
            ('enthalpy_correction_hartree', rx_hcorr),
            ('gibbs_correction_hartree', rx_gcorr),
            ('zpe_hartree', rx_zpe),
            ('gibbs_hartree', rx_g),
            ('enthalpy_hartree', rx_h),
            ('entropy_term_hartree', rx_entropy_term),
            ('entropy_correction_hartree', rx_entropy_corr),
            ('S_vib_cal_mol_K', rx_svib),
            ('S_rot_cal_mol_K', rx_srot),
            ('S_trans_cal_mol_K', rx_strans),
            ('S_elec_cal_mol_K', rx_selec),
        ):
            mm = rx.search(stripped)
            if mm:
                thermo[key] = _float(mm.group('value'))

        m = rx_temp.search(stripped)
        if m: thermo['temperature_K'] = _float(m.group('value'))
        m = rx_press.search(stripped)
        if m: thermo['pressure_atm'] = _float(m.group('value'))
        m = rx_qrrho.search(stripped)
        if m: thermo['thermo_treatment'] = m.group('treatment')
        m = rx_qcut.search(stripped)
        if m: thermo['rrho_cutoff_cm1'] = _float(m.group('value'))

        m = rx_charge.search(stripped)
        if m: charge = int(m.group('value'))
        m = rx_mult.search(stripped)
        if m: multiplicity = int(m.group('value'))
        m = rx_solvation.search(stripped)
        if m: solvation = m.group('model')
        m = rx_dipole.search(stripped)
        if m: dipole = _float(m.group('value'))
        m = rx_s2.search(stripped)
        if m: s2_actual = _float(m.group('value'))
        m = rx_s2i.search(stripped)
        if m: s2_ideal = _float(m.group('value'))

        m = rx_sp.search(stripped)
        if m:
            final_energy = _float(m.group('value'))
            if final_energy is not None:
                optimization_energies.append(final_energy)

        if rx_normal.search(stripped):
            normal = True
        if rx_fatal.search(stripped):
            normal = False
            errors.append(stripped)

        # ---------- section-state handlers ----------
        if state == 'COORDINATES':
            if table_noise(stripped):
                continue
            c = coord_from_line(stripped)
            if c:
                current_geometry.append(c)
                continue
            if current_geometry:
                if coord_unit == 'angstrom' or not final_geometry:
                    final_geometry = current_geometry[:]
            current_geometry = []
            state = 'SEARCHING'
            # fall through to re-dispatch this line

        if state == 'ORBITALS':
            if rx_spin_up.search(stripped):
                current_spin = 'alpha'
                continue
            if rx_spin_dn.search(stripped):
                current_spin = 'beta'
                continue
            if rx_orbsec.search(stripped) or table_noise(stripped):
                continue
            m = rx_orbrow.search(stripped)
            if m:
                eh = _float(m.group('eh'))
                ev = _float(m.group('ev')) if m.group('ev') is not None else (eh * HARTREE_TO_EV if eh is not None else None)
                current_orbitals.append({
                    'index': int(m.group('idx')), 'occ': _float(m.group('occ')),
                    'eh': eh, 'ev': ev, 'spin': current_spin
                })
                continue
            if current_orbitals:
                orbitals_all = current_orbitals[:]
            current_orbitals = []
            current_spin = 'restricted'
            state = 'SEARCHING'

        if state == 'TDDFT':
            if rx_tdsoc.search(stripped):
                if td_cm:
                    state = 'SEARCHING'
                continue
            if rx_tdsec.search(stripped) or table_noise(stripped):
                continue
            m = rx_tdtrans.search(stripped) or rx_td.search(stripped)
            if m:
                cmv = _float(m.group('cm'))
                fosc = _float(m.group('fosc'))
                # Proper ORCA absorption rows have non-negative oscillator strengths.
                if cmv is not None and cmv > 0 and fosc is not None and fosc >= 0:
                    td_cm.append(cmv)
                    td_fosc.append(fosc)
                continue
            if td_cm or major(stripped):
                state = 'SEARCHING'

        if state == 'FREQUENCIES':
            if rx_freqsec.search(stripped) or (set(stripped) <= {'-','='}):
                continue
            m = rx_freqrow.search(stripped)
            if m:
                v = _float(m.group('cm'))
                if v is not None:
                    if m.group('imag') and v > 0:
                        v = -v
                    freqs.append(v)
                    if v < 0:
                        imag.append(v)
                continue
            # ORCA_ENGINE re-dispatches any recognized section header.
            if (rx_irsec.search(stripped) or rx_tdsec.search(stripped) or rx_coord.search(stripped)
                or rx_orbsec.search(stripped) or rx_hirsh.search(stripped) or rx_mull.search(stripped)
                or rx_loew.search(stripped) or rx_chelpg.search(stripped) or rx_mayer.search(stripped)
                or major(stripped) or 'THERMOCHEMISTRY' in stripped.upper()):
                state = 'SEARCHING'
            else:
                continue

        if state == 'IR':
            if rx_irsec.search(stripped) or (set(stripped) <= {'-','='}):
                continue
            u = stripped.upper()
            if 'MODE' in u and 'FREQ' in u:
                ir_has_epsilon = bool(re.search(r'\bEPS\b', u))
                continue
            m = rx_irrow.search(stripped)
            if m:
                freq = _float(m.group('freq'))
                # ORCA 6.1 prints: mode, frequency, eps, Int (km/mol), T**2.
                # A short legacy row can instead print intensity directly
                # after frequency. Never plot eps as a km/mol intensity.
                column = 'second' if (ir_has_epsilon is True or
                                      (ir_has_epsilon is None and m.group('third') is not None)) else 'first'
                inten = _float(m.group(column))
                if freq is not None and inten is not None:
                    ir.append((freq, inten))
                    if freq < 0 and freq not in imag:
                        imag.append(freq)
                    if len(freqs) < len(ir):
                        freqs.append(freq)
                continue
            if (rx_tdsec.search(stripped) or rx_freqsec.search(stripped) or rx_coord.search(stripped)
                or rx_orbsec.search(stripped) or rx_hirsh.search(stripped) or rx_mull.search(stripped)
                or rx_loew.search(stripped) or rx_chelpg.search(stripped) or rx_mayer.search(stripped)
                or major(stripped) or 'THERMOCHEMISTRY' in u):
                state = 'SEARCHING'
            else:
                continue

        if state == 'CHARGES':
            u = stripped.upper()
            # A new recognized section ends charge parsing.
            if (rx_coord.search(stripped) or rx_orbsec.search(stripped) or rx_tdsec.search(stripped)
                or rx_freqsec.search(stripped) or rx_irsec.search(stripped) or major(stripped)):
                state = 'SEARCHING'
            elif 'SUM OF ATOMIC CHARGES' in u or 'SUM OF CHARGES' in u:
                state = 'SEARCHING'
                continue
            elif u.startswith('TOTAL CHARGES') or u.startswith('ATOM') or ('ZA' in u and 'QA' in u) or set(stripped) <= {'-','='}:
                continue
            else:
                if charge_type == 'mayer':
                    mm = rx_mayerrow.search(stripped)
                    if mm:
                        atomic_charge_sets.setdefault('mayer', []).append(_float(mm.group('qa')))
                        atomic_charge_sets.setdefault('mayer_valence', []).append(_float(mm.group('va')) or 0.0)
                        continue
                mm = rx_chg_colon.search(stripped) or rx_chg_table.search(stripped)
                if mm:
                    atomic_charge_sets.setdefault(charge_type or 'unknown', []).append(_float(mm.group('charge')))
                    continue
                if not table_noise(stripped):
                    state = 'SEARCHING'

        # ---------- SEARCHING dispatch ----------
        if state != 'SEARCHING':
            continue

        if expecting_basis:
            basis = stripped
            expecting_basis = False
            continue

        m = rx_basis.search(stripped)
        if m:
            b = m.group('basis').strip()
            if b: basis = b
            else: expecting_basis = True
            continue

        m = rx_exchange.search(stripped)
        if m:
            exchange = m.group('value')
            if not method or method in ('DFT','HF'):
                method = exchange
            elif exchange not in method:
                method = method + '/' + exchange
            continue

        m = rx_corr.search(stripped)
        if m:
            correlation = m.group('value')
            if not method or method in ('DFT','HF'):
                method = correlation
            elif correlation not in method:
                method = method + '/' + correlation
            continue

        if re.search(r'\bDFT\s+CALCULATION\b', stripped, re.I):
            if not method: method = 'DFT'
            continue
        if re.search(r'\bHF\s+CALCULATION\b', stripped, re.I):
            if not method: method = 'HF'
            continue
        if re.search(r'\bCOMPOSITE\s+CALCULATION\b', stripped, re.I):
            method = 'Composite'
            continue

        m = rx_disp.search(stripped)
        if m:
            dispersion = m.group('value') or 'Present'
            continue

        m = rx_smd.search(stripped)
        if m:
            solvation = 'SMD'; solvent = m.group('solvent')
            continue
        m = rx_cpcm.search(stripped)
        if m:
            solvation = 'CPCM'; solvent = m.group('solvent')
            continue
        m = rx_solvent.search(stripped)
        if m:
            solvent = m.group('solvent')
            continue

        if rx_coord.search(stripped):
            if rx_coord_ang.search(stripped):
                coord_unit = 'angstrom'
            elif rx_coord_au.search(stripped):
                # ORCA_ENGINE keeps Angstrom coordinates when both are printed.
                if final_geometry:
                    continue
                coord_unit = 'bohr'
            else:
                coord_unit = None
            current_geometry = []
            state = 'COORDINATES'
            continue

        if rx_spin_up.search(stripped):
            current_spin = 'alpha'; state = 'ORBITALS'
            continue
        if rx_spin_dn.search(stripped):
            current_spin = 'beta'; state = 'ORBITALS'
            continue
        if rx_orbsec.search(stripped):
            current_spin = 'restricted'
            current_orbitals = []
            state = 'ORBITALS'
            continue

        if rx_tdsoc.search(stripped):
            # ORCA_ENGINE deliberately excludes SOC-corrected table from the
            # ordinary electric-dipole spectrum to prevent concatenation.
            continue
        if rx_tdsec.search(stripped):
            td_cm, td_fosc = [], []
            state = 'TDDFT'
            continue

        if rx_freqsec.search(stripped):
            freqs, imag = [], []
            state = 'FREQUENCIES'
            continue

        if rx_irsec.search(stripped):
            ir = []
            ir_has_epsilon = None
            state = 'IR'
            continue

        if rx_hirsh.search(stripped):
            charge_type='hirshfeld'; atomic_charge_sets['hirshfeld']=[]; state='CHARGES'; continue
        if rx_mull.search(stripped):
            charge_type='mulliken'; atomic_charge_sets['mulliken']=[]; state='CHARGES'; continue
        if rx_loew.search(stripped):
            charge_type='loewdin'; atomic_charge_sets['loewdin']=[]; state='CHARGES'; continue
        if rx_chelpg.search(stripped):
            charge_type='chelpg'; atomic_charge_sets['chelpg']=[]; state='CHARGES'; continue
        if rx_mayer.search(stripped):
            charge_type='mayer'; atomic_charge_sets['mayer']=[]; atomic_charge_sets['mayer_valence']=[]; state='CHARGES'; continue

    # Flush unfinished sections at EOF.
    if state == 'COORDINATES' and current_geometry:
        if coord_unit == 'angstrom' or not final_geometry:
            final_geometry = current_geometry[:]
    if state == 'ORBITALS' and current_orbitals:
        orbitals_all = current_orbitals[:]

    # The echoed SimpleInput method is authoritative for names such as PBE0.
    if input_method:
        method = input_method

    # Reconstruct total H/G like ORCA_ENGINE when only corrections are present.
    if thermo.get('enthalpy_hartree') is None and final_energy is not None and thermo.get('enthalpy_correction_hartree') is not None:
        thermo['enthalpy_hartree'] = final_energy + thermo['enthalpy_correction_hartree']
    if thermo.get('gibbs_hartree') is None and final_energy is not None and thermo.get('gibbs_correction_hartree') is not None:
        thermo['gibbs_hartree'] = final_energy + thermo['gibbs_correction_hartree']
    if thermo.get('entropy_term_hartree') is None and thermo.get('entropy_correction_hartree') is not None:
        thermo['entropy_term_hartree'] = -thermo['entropy_correction_hartree']

    # Derived units only; raw ORCA values remain intact.
    for k in list(thermo):
        if k.endswith('_hartree') and thermo[k] is not None:
            thermo[k.replace('_hartree','_kj_mol')] = thermo[k] * HARTREE_TO_KJMOL
    sent = [thermo.get(k) for k in ('S_vib_cal_mol_K','S_rot_cal_mol_K','S_trans_cal_mol_K','S_elec_cal_mol_K') if thermo.get(k) is not None]
    if sent:
        thermo['entropy_cal_mol_K'] = sum(sent)
        thermo['entropy_J_mol_K'] = sum(sent) * 4.184
    elif (thermo.get('temperature_K') is not None and thermo['temperature_K'] > 0
          and thermo.get('enthalpy_hartree') is not None and thermo.get('gibbs_hartree') is not None):
        # ORCA's "Final entropy term" is T*S in Eh, not S in Eh/K.
        thermo['entropy_J_mol_K'] = (
            (thermo['enthalpy_hartree'] - thermo['gibbs_hartree'])
            * HARTREE_TO_KJMOL * 1000.0 / thermo['temperature_K']
        )

    energies = {}
    if final_energy is not None:
        energies = {
            'final_energy_hartree': final_energy,
            'final_energy_kj_mol': final_energy * HARTREE_TO_KJMOL,
            'final_energy_ev': final_energy * HARTREE_TO_EV,
        }

    # TDDFT: ORCA_ENGINE stores cm^-1 + fosc. Convert only after exact parsing.
    td = []
    for idx, (cmv, fosc) in enumerate(zip(td_cm, td_fosc), 1):
        if cmv and cmv > 0 and fosc is not None and fosc >= 0:
            td.append({
                'state': idx,
                'cm1': cmv,
                'ev': cmv / 8065.544005,
                'nm': 1.0e7 / cmv,
                'f': fosc,
            })

    # Frontier orbitals from final retained ORBITAL ENERGIES section.
    occupied = [o for o in orbitals_all if (o.get('occ') or 0.0) > 1e-8 and o.get('ev') is not None]
    virtual = [o for o in orbitals_all if (o.get('occ') or 0.0) <= 1e-8 and o.get('ev') is not None]
    homo = occupied[-1]['ev'] if occupied else None
    lumo = virtual[0]['ev'] if virtual else None
    orb = {
        'orbitals': orbitals_all[-500:],
        'homo_ev': homo,
        'lumo_ev': lumo,
        'gap_ev': (lumo - homo) if homo is not None and lumo is not None else None,
    }

    # ORCA_ENGINE classifies completeness against total 3N frequency records.
    real_atoms = [a for a in final_geometry if not str(a.get('element','')).endswith(':') and str(a.get('element','')).upper() != 'DA']
    expected = 3 * len(real_atoms) if len(real_atoms) >= 2 else (0 if real_atoms else None)
    if normal is False:
        sp_status='FAILED_CALCULATION'; thermo_rel='FAILED_CALCULATION'
    elif normal is not True:
        sp_status='UNCONFIRMED_CALCULATION'; thermo_rel='UNCONFIRMED_CALCULATION'
    elif not freqs:
        sp_status='NO_FREQUENCY_CALCULATION'; thermo_rel='ELECTRONIC_ONLY'
    elif expected is not None and expected > 0 and len(freqs) < expected:
        sp_status='INCOMPLETE_FREQUENCIES'; thermo_rel='INCOMPLETE_FREQUENCIES'
    elif len(imag) == 0:
        sp_status='LIKELY_MINIMUM'; thermo_rel='HIGH'
    elif len(imag) == 1:
        sp_status='TRANSITION_STATE'; thermo_rel='TRANSITION_STATE'
    else:
        sp_status='HIGHER_ORDER_SADDLE'; thermo_rel='UNRELIABLE_FOR_MINIMUM'

    # Keep charge sets separated instead of mixing Mulliken/Hirshfeld/etc.
    atomic_charges = []
    preferred = None
    for name in ('hirshfeld','mulliken','loewdin','chelpg','mayer'):
        if atomic_charge_sets.get(name):
            preferred = name
            atomic_charges = atomic_charge_sets[name]
            break

    system = {'charge':charge, 'multiplicity':multiplicity}
    if s2_actual is not None: system['s2_actual'] = s2_actual
    if s2_ideal is not None: system['s2_ideal'] = s2_ideal

    return {
        'filename': filename,
        'engine': 'ORCA',
        'version': version,
        'normal_termination': normal is True,
        'errors': errors,
        'method': method,
        'method_components': {'exchange': exchange, 'correlation': correlation},
        'basis': basis,
        'dispersion': dispersion,
        'solvation_model': solvation,
        'solvent': solvent,
        'energies': energies,
        'optimization_energies': optimization_energies[-500:],
        'thermochemistry': thermo,
        'frequencies_cm1': freqs[-5000:],
        'imaginary_frequencies_cm1': imag[-5000:],
        'expected_frequency_count': expected,
        'stationary_point_status': sp_status,
        'thermochemistry_reliability': thermo_rel,
        'ir_spectrum': ir[-5000:],
        'tddft_states': td[:5000],
        'orbitals': orb,
        'dipole': dipole,
        'atomic_charges': atomic_charges,
        'atomic_charge_type': preferred,
        'atomic_charge_sets': atomic_charge_sets,
        'final_geometry': final_geometry,
        'raman_spectrum': parse_raman(text),
        'system': system,
        'timings': parse_timings(text,'ORCA'),
    }


def _parse_orca_precise(text, filename):
    """Parse ORCA using ORCA_ENGINE-compatible logical job blocks.

    The final job is the primary Telegram result, exactly as the existing UI
    expects, while job count and compact block summaries are retained for
    diagnostics and multi-job outputs.
    """
    blocks = _orca_split_jobs(text)
    parsed = [_orca_parse_one_job(b, filename) for b in blocks]
    parsed = [p for p in parsed if p]
    if not parsed:
        return _orca_parse_one_job(text, filename)
    primary = parsed[-1]
    primary['orca_job_blocks_count'] = len(parsed)
    if len(parsed) > 1:
        primary['orca_job_blocks'] = [
            {
                'index': i+1,
                'method': p.get('method'),
                'basis': p.get('basis'),
                'normal_termination': p.get('normal_termination'),
                'final_energy_hartree': p.get('energies',{}).get('final_energy_hartree'),
                'tddft_states': len(p.get('tddft_states',[])),
                'frequencies': len(p.get('frequencies_cm1',[])),
            } for i,p in enumerate(parsed)
        ]
    return primary

def _parse_psi4_vibrations(text):
    freqs=[]; ir=[]; pending=[]
    for line in text.splitlines():
        if re.search(r'Freq\s*\[cm\^-1\]',line,re.I):
            vals=[]
            for tok in line.split(']')[-1].split():
                t=tok.strip(); im=t.lower().endswith('i'); t=t[:-1] if im else t
                try: vals.append(-abs(float(t.replace('D','E'))) if im else float(t.replace('D','E')))
                except Exception: pass
            if vals: pending=vals; freqs.extend(vals)
        elif pending and re.search(r'IR\s+activ\s*\[km/mol\]',line,re.I):
            vals=[]
            for tok in line.split(']')[-1].split():
                try: vals.append(float(tok.replace('D','E')))
                except Exception: pass
            ir.extend(list(zip(pending,vals))); pending=[]
    return freqs,ir

def _parse_psi4_tddft(text):
    states=[]; active=False
    for line in text.splitlines():
        u=line.upper()
        if 'EXCITATION ENERGY' in u and 'OSCILLATOR STRENGTH' in u:
            states=[]; active=True; continue
        if active:
            m=re.match(r'^\s*(\d+)\s+',line)
            if not m:
                if states and line.strip() and '----' not in line: active=False
                continue
            tail=line[m.end():]
            # The symmetry label contains a number, e.g. "A->A (1 A)".
            # Numeric columns start only after that label: excitation Eh,
            # excitation eV, total Eh, f(length), f(velocity), ...
            if ')' in tail:
                tail=tail.rsplit(')',1)[1]
            else:
                columns=re.split(r'\s{2,}',tail.strip())
                if len(columns)>1: tail=' '.join(columns[1:])
            vals=_nums(tail)
            if len(vals)>=4:
                au,ev,total_au,fosc=vals[:4]
                if (all(v is not None and math.isfinite(v) for v in (au,ev,total_au,fosc))
                    and au>0 and ev>0 and fosc>=0
                    and abs(ev-au*HARTREE_TO_EV)<max(0.05,0.02*ev)):
                    states.append({'state':int(m.group(1)),'ev':ev,'nm':1239.841984/ev,
                                   'f':fosc,'f_velocity':vals[4] if len(vals)>4 else None,
                                   'excitation_au':au,'total_energy_au':total_au})
    return states

def _parse_psi4_precise(text, filename):
    F=FLOAT_RE
    version=None
    for rx in [r'Psi4\s+([0-9][\w.\-]+)',r'Psi4\s+Version\s*[:=]?\s*([0-9][\w.\-]+)']:
        m=re.search(rx,text,re.I)
        if m: version=m.group(1); break
    success_marks=list(re.finditer(r'Psi4\s+exiting successfully',text,re.I))
    fatal_pattern=r'(PSIEXCEPTION|TRACEBACK|FATAL ERROR|SEGMENTATION FAULT|OUT OF MEMORY|CONVERGENCE FAILURE)'
    fatal_marks=list(re.finditer(fatal_pattern,text,re.I))
    normal=bool(success_marks and (not fatal_marks or success_marks[-1].start()>fatal_marks[-1].start()))
    fatal=[ln.strip() for ln in text.splitlines() if re.search(fatal_pattern,ln,re.I)]
    fm=list(re.finditer(r'@(?P<method>(?:DF-)?(?:RHF|ROHF|UHF|RKS|UKS|SCF|MP2|CCSD(?:\(T\))?))\s+Final Energy:\s*(?P<e>'+F+r')',text,re.I))
    method=fm[-1].group('method') if fm else None
    basis=None
    for rx in [r'BASIS\s*=\s*([^\s]+)',r'\bbasis\s+([A-Za-z0-9+*()_\-]+)',r'Basis Set:\s*([^\n]+)']:
        ms=list(re.finditer(rx,text,re.I))
        if ms: basis=ms[-1].group(1).strip(); break
    energies={}; final=_float(fm[-1].group('e')) if fm else _last_number(text,[r'Final Energy\s*[:=]\s*('+F+r')'])
    if final is not None: energies.update(final_energy_hartree=final,final_energy_kj_mol=final*HARTREE_TO_KJMOL,final_energy_ev=final*HARTREE_TO_EV)
    for key,rx in {'reference_energy_hartree':r'Reference Energy\s*=\s*('+F+r')','correlation_energy_hartree':r'Correlation Energy\s*=\s*('+F+r')','nuclear_repulsion_hartree':r'Nuclear Repulsion Energy\s*=\s*('+F+r')'}.items():
        v=_last_number(text,[rx]);
        if v is not None: energies[key]=v
    opt=[]
    for m in re.finditer(r'@(?:DF-)?(?:RHF|ROHF|UHF|RKS|UKS|SCF)\s+Final Energy:\s*('+F+r')',text,re.I):
        v=_float(m.group(1))
        if v is not None and (not opt or abs(v-opt[-1])>1e-12): opt.append(v)
    freqs,ir=_parse_psi4_vibrations(text); td=_parse_psi4_tddft(text); tr={}
    for k,rx in {'energy_0K_hartree':r'(?m)^\s*Energy \(0 K\)\s+'+F+r'\s+('+F+r')\s*$','internal_energy_hartree':r'(?m)^\s*Internal energy\s+'+F+r'\s+('+F+r')\s*$','enthalpy_hartree':r'(?m)^\s*Enthalpy\s+'+F+r'\s+('+F+r')\s*$','gibbs_hartree':r'(?m)^\s*Gibbs Free Energy\s+'+F+r'\s+('+F+r')\s*$'}.items():
        v=_last_number(text,[rx]);
        if v is not None: tr[k]=v
    mh=list(re.finditer(r'Total H, Enthalpy at\s*(?P<T>[0-9.]+)\s*\[K\]\s*(?P<value>'+F+r')\s*\[Eh\]',text,re.I)); mg=list(re.finditer(r'Total G, Free enthalpy at\s*(?P<T>[0-9.]+)\s*\[K\]\s*(?P<value>'+F+r')\s*\[Eh\]',text,re.I))
    if mh: tr['temperature_K']=_float(mh[-1].group('T')); tr['enthalpy_hartree']=_float(mh[-1].group('value'))
    if mg: tr['temperature_K']=_float(mg[-1].group('T')); tr['gibbs_hartree']=_float(mg[-1].group('value'))
    zpe=_last_number(text,[r'(?:Zero[- ]point(?: vibrational)? energy|ZPE(?:_vib)?)\s*(?:=|:)?.*?('+F+r')\s*\[?Eh\]?'])
    if zpe is not None: tr['zpe_hartree']=zpe
    for k in list(tr):
        if k.endswith('_hartree'): tr[k.replace('_hartree','_kj_mol')]=tr[k]*HARTREE_TO_KJMOL
    blocks=[]; cur=[]; active=False
    for line in text.splitlines():
        if re.search(r'(?:Final optimized geometry|Geometry \(in Angstrom\)|Cartesian Geometry \(in Angstrom\))',line,re.I):
            if cur: blocks.append(cur)
            cur=[]; active=True; continue
        if active:
            m=re.match(r'^\s*(?:\d+\s+)?([A-Za-z]{1,3})\s+('+F+r')\s+('+F+r')\s+('+F+r')\s*$',line)
            if m: cur.append({'element':m.group(1),'x':_float(m.group(2)),'y':_float(m.group(3)),'z':_float(m.group(4))}); continue
            if cur and not line.strip(): blocks.append(cur); cur=[]; active=False
    if cur: blocks.append(cur)
    geometry=blocks[-1] if blocks else []
    imag=[x for x in freqs if x<0]
    if not normal and fatal: sp='FAILED_CALCULATION'; rel='FAILED_CALCULATION'
    elif not normal: sp='UNCONFIRMED_CALCULATION'; rel='UNCONFIRMED_CALCULATION'
    elif not freqs: sp='NO_FREQUENCY_CALCULATION'; rel='ELECTRONIC_ONLY'
    elif len(imag)==0: sp='LIKELY_MINIMUM'; rel='HIGH'
    elif len(imag)==1: sp='TRANSITION_STATE'; rel='TRANSITION_STATE'
    else: sp='HIGHER_ORDER_SADDLE'; rel='UNRELIABLE_FOR_MINIMUM'
    return {'filename':filename,'engine':'Psi4','version':version,'normal_termination':normal,'errors':fatal,'method':method,'basis':basis,'energies':energies,'optimization_energies':opt[-500:],'thermochemistry':tr,'frequencies_cm1':freqs,'imaginary_frequencies_cm1':imag,'stationary_point_status':sp,'thermochemistry_reliability':rel,'ir_spectrum':ir,'tddft_states':td,'orbitals':parse_orbitals(text,'Psi4'),'dipole':parse_dipole(text),'atomic_charges':[],'final_geometry':geometry,'raman_spectrum':[],'system':parse_charge_mult(text),'timings':parse_timings(text,'Psi4')}

def _normalize_analysis_schema(a):
    """Normalize ORCA/Psi4 parser output to the stable Telegram UI schema.

    ORCA_ENGINE intentionally stores some observables in compact scientific
    forms (e.g. dipole as a scalar and charge arrays as floats).  The Telegram
    presentation layer historically expects richer dictionaries.  This adapter
    is the single compatibility boundary between the scientific parser and UI.
    """
    if not isinstance(a, dict):
        raise TypeError(f"Parser returned {type(a).__name__}, expected dict")

    # Mapping-like sections used by summary/report functions.
    for key in ('energies','thermochemistry','system','timings'):
        if not isinstance(a.get(key), dict):
            a[key] = {}

    # Dipole: ORCA_ENGINE-compatible parser stores magnitude as float.
    d = a.get('dipole')
    if isinstance(d, (int, float)):
        a['dipole'] = {'magnitude_debye': float(d)}
    elif d is None:
        a['dipole'] = {}
    elif not isinstance(d, dict):
        a['dipole'] = {'raw': str(d)}

    # Geometry must be a list of atom dictionaries.
    geom = a.get('final_geometry')
    if not isinstance(geom, list):
        geom = []
    clean_geom = []
    for atom in geom:
        if isinstance(atom, dict):
            clean_geom.append(atom)
        elif isinstance(atom, (list, tuple)) and len(atom) >= 4:
            clean_geom.append({'element': str(atom[0]), 'x': _float(atom[1]), 'y': _float(atom[2]), 'z': _float(atom[3])})
    a['final_geometry'] = clean_geom
    geom = clean_geom

    # Atomic charges: ORCA_ENGINE exposes per-scheme arrays of floats.  Adapt
    # the selected array to UI records while preserving atomic_charge_sets.
    charges = a.get('atomic_charges')
    normalized_charges = []
    if isinstance(charges, list):
        for i, item in enumerate(charges):
            if isinstance(item, dict):
                q = dict(item)
                q.setdefault('index', i)
                if 'element' not in q:
                    q['element'] = geom[i].get('element','?') if i < len(geom) else '?'
                if 'charge' in q and q['charge'] is not None:
                    q['charge'] = float(q['charge'])
                normalized_charges.append(q)
            elif isinstance(item, (int, float)):
                normalized_charges.append({
                    'index': i,
                    'element': geom[i].get('element','?') if i < len(geom) else '?',
                    'charge': float(item),
                })
    a['atomic_charges'] = normalized_charges

    # Orbitals are always exposed as {orbitals:[...], homo_ev, lumo_ev, gap_ev}.
    orb = a.get('orbitals')
    if isinstance(orb, list):
        orb = {'orbitals': orb}
    elif not isinstance(orb, dict):
        orb = {'orbitals': []}
    arr = orb.get('orbitals')
    if not isinstance(arr, list):
        arr = []
    clean_orbs = []
    for i, item in enumerate(arr):
        if isinstance(item, dict):
            clean_orbs.append(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            clean_orbs.append({'index': i, 'occ': _float(item[0]), 'eh': _float(item[1]), 'ev': _float(item[2])})
    orb['orbitals'] = clean_orbs
    a['orbitals'] = orb

    # TD-DFT states must be dictionaries.  Never pass stray scalar values to
    # spectrum/report code; this also prevents physically impossible negative
    # sticks from malformed table captures.
    states = a.get('tddft_states')
    clean_states = []
    if isinstance(states, list):
        for i, item in enumerate(states, 1):
            if not isinstance(item, dict):
                continue
            s = dict(item)
            nm = _float(s.get('nm'))
            ev = _float(s.get('ev'))
            cm1 = _float(s.get('cm1'))
            fosc = _float(s.get('f'))
            if nm is None and cm1 and cm1 > 0: nm = 1.0e7 / cm1
            if nm is None and ev and ev > 0: nm = 1239.841984 / ev
            if ev is None and cm1 and cm1 > 0: ev = cm1 / 8065.544005
            if fosc is None or fosc < 0: continue
            if nm is None or nm <= 0: continue
            s['state'] = int(s.get('state') or i)
            s['nm'] = nm
            s['ev'] = ev
            if cm1 is not None: s['cm1'] = cm1
            s['f'] = fosc
            clean_states.append(s)
    a['tddft_states'] = clean_states

    # IR/Raman are normalized to (x, intensity) pairs.
    for key, xkeys, ykeys in (
        ('ir_spectrum', ('frequency_cm','frequency_cm1','freq','cm1'), ('intensity_km_mol','intensity','t2','activity')),
        ('raman_spectrum', ('frequency_cm','frequency_cm1','freq','cm1'), ('activity','intensity','raman_activity')),
    ):
        data = a.get(key)
        clean = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    x = next((_float(item.get(k)) for k in xkeys if item.get(k) is not None), None)
                    y = next((_float(item.get(k)) for k in ykeys if item.get(k) is not None), None)
                    if x is not None: clean.append((x, 0.0 if y is None else y))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    x, y = _float(item[0]), _float(item[1])
                    if x is not None: clean.append((x, 0.0 if y is None else y))
        a[key] = clean

    for key in ('frequencies_cm1','imaginary_frequencies_cm1','optimization_energies'):
        vals = a.get(key)
        if not isinstance(vals, list):
            a[key] = []
        else:
            a[key] = [float(v) for v in vals if isinstance(v,(int,float))]

    if not isinstance(a.get('errors'), list):
        a['errors'] = [] if a.get('errors') is None else [str(a.get('errors'))]
    return a


def parse_output_text(text, filename='calculation.out'):
    eng=detect_engine(text)
    if eng=='ORCA':
        result = _parse_orca_precise(text,filename)
    elif eng=='Psi4':
        result = _parse_psi4_precise(text,filename)
    else:
        normal,errors=parse_status(text,eng); method,basis=parse_method_basis(text,eng); freqs,ir=parse_frequencies(text,eng)
        result = {'filename':filename,'engine':eng,'version':detect_version(text,eng),'normal_termination':normal,'errors':errors,'method':method,'basis':basis,'energies':parse_energies(text,eng),'optimization_energies':parse_optimization(text,eng),'thermochemistry':parse_thermo(text,eng),'frequencies_cm1':freqs,'imaginary_frequencies_cm1':[x for x in freqs if x<0],'ir_spectrum':ir,'tddft_states':parse_tddft(text,eng),'orbitals':parse_orbitals(text,eng),'dipole':parse_dipole(text),'atomic_charges':parse_atomic_charges(text),'final_geometry':parse_final_geometry(text),'raman_spectrum':parse_raman(text),'system':parse_charge_mult(text),'timings':parse_timings(text,eng)}
    return _normalize_analysis_schema(result)

def _gaussian_curve(points, xmin=None, xmax=None, sigma=10.0, normalize=True, n=3000):
    import numpy as np
    if not points: return None,None
    xs=[float(x) for x,y in points if x is not None and y is not None]
    if not xs: return None,None
    lo=xmin if xmin is not None else min(xs)-6*sigma; hi=xmax if xmax is not None else max(xs)+6*sigma
    grid=np.linspace(lo,hi,n); yy=np.zeros_like(grid)
    for x,y in points: yy += max(0.0,float(y or 0.0))*np.exp(-0.5*((grid-float(x))/sigma)**2)
    if normalize and yy.max()>0: yy=yy/yy.max()
    return grid,yy

def make_overlay_plot(analyses, kind, outpath, normalize=True, sigma=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    fig=plt.figure(figsize=(10,6))
    ax=fig.add_subplot(111)
    used=0

    if kind=='uv':
        sigma=10.0 if sigma is None else float(sigma)
        pts_all=[]
        for a in analyses:
            pts=[(float(s.get('nm')), max(0.0, float(s.get('f') or 0.0))) for s in a.get('tddft_states',[]) if s.get('nm') and s.get('f') is not None]
            if pts:
                pts_all.extend(pts)
        if not pts_all:
            plt.close(fig)
            return None
        allnm=[p[0] for p in pts_all]
        lo=max(150.0,min(allnm)-60.0)
        hi=min(1200.0,max(allnm)+60.0)
        for a in analyses:
            pts=[(float(s.get('nm')), max(0.0, float(s.get('f') or 0.0))) for s in a.get('tddft_states',[]) if s.get('nm') and s.get('f') is not None]
            if not pts:
                continue
            x,y=_gaussian_curve(pts,lo,hi,sigma=sigma,normalize=normalize,n=3000)
            ax.plot(x,y,lw=1.8,label=Path(a.get('filename','spectrum')).stem)
            used+=1
        ax.set_xlim(lo,hi)
        ax.set_xlabel('Wavelength (nm)')
        ax.set_ylabel('Relative intensity' if normalize else 'Oscillator-strength weighted intensity')
        ax.set_title('TD-DFT / UV-Vis overlay')
        ax.set_ylim(bottom=0)
    else:
        sigma=16.0 if sigma is None else float(sigma)
        pool=[]
        for a in analyses:
            pts=[(float(x), abs(float(y or 0.0))) for x,y in a.get('ir_spectrum',[]) if x is not None and float(x)>0]
            if pts:
                pool.extend(pts)
        if not pool:
            plt.close(fig)
            return None
        pool_mid=[(f,i) for f,i in pool if 400.0 <= f <= 4000.0]
        use_pool = pool_mid if pool_mid else pool
        lo=400.0 if any(400.0 <= f <= 4000.0 for f,_ in use_pool) else max(0.0, min(f for f,_ in use_pool)-80.0)
        hi=4000.0 if any(400.0 <= f <= 4000.0 for f,_ in use_pool) else max(f for f,_ in use_pool)+80.0
        for a in analyses:
            pts=[(float(x), abs(float(y or 0.0))) for x,y in a.get('ir_spectrum',[]) if x is not None and float(x)>0]
            pts=[(f,i) for f,i in pts if lo <= f <= hi]
            if not pts:
                continue
            x,y=_gaussian_curve(pts,lo,hi,sigma=sigma,normalize=True,n=3600)
            trans = 100.0 - 92.0 * y
            ax.plot(x,trans,lw=1.5,label=Path(a.get('filename','spectrum')).stem)
            used+=1
        ax.set_xlim(hi,lo)
        ax.set_xlabel('Wavenumber (cm$^{-1}$)')
        ax.set_ylabel('Transmittance (%)')
        ax.set_title('Simulated FT-IR overlay')
        ax.set_ylim(0,102)
    if used<2:
        plt.close(fig)
        return None
    ax.legend(fontsize=8, frameon=False)
    ax.grid(alpha=.18)
    fig.tight_layout()
    fig.savefig(outpath,dpi=300,bbox_inches='tight')
    plt.close(fig)
    return outpath
'''

# v5.8 analyzer additions
ANALYZER_V58_PATCH_CODE = r'''def conceptual_dft_descriptors(a):
    # ORCA_ENGINE-compatible frontier-orbital descriptors.
    o=(a or {}).get('orbitals',{}) or {}
    h=o.get('homo_ev'); l=o.get('lumo_ev')
    try:
        h=float(h); l=float(l)
    except Exception:
        return {}
    if not (math.isfinite(h) and math.isfinite(l)) or l<=h:
        return {}
    gap=l-h; ip=-h; ea=-l; eta=gap/2.0; mu=(h+l)/2.0
    out={
        'ionization_potential_ev': ip,
        'electron_affinity_ev': ea,
        'chemical_hardness_ev': eta,
        'chemical_potential_ev': mu,
        'electronegativity_ev': -mu,
        'chemical_softness_ev': (1.0/gap if gap>0 else None),
        'electrophilicity_index_ev': ((mu*mu)/(2.0*eta) if eta>0 else None),
        'electrodonating_power_ev': (((3.0*ip+ea)**2)/(16.0*(ip-ea)) if (ip-ea)>0 else None),
        'electroaccepting_power_ev': (((ip+3.0*ea)**2)/(16.0*(ip-ea)) if (ip-ea)>0 else None),
    }
    wp=out.get('electroaccepting_power_ev'); wm=out.get('electrodonating_power_ev')
    out['net_electrophilicity_ev']=(wp+wm) if wp is not None and wm is not None else None
    return out

_parse_output_text_v57=parse_output_text
def parse_output_text(text,filename='output.out'):
    a=_parse_output_text_v57(text,filename)
    a['conceptual_dft']=conceptual_dft_descriptors(a)
    return a

_section_summary_v57=section_summary
def section_summary(a):
    base=_section_summary_v57(a)
    d=a.get('conceptual_dft',{}) or {}
    extra=[]
    for k,label in [('ionization_potential_ev','I'),('electron_affinity_ev','A'),('chemical_hardness_ev','eta'),('chemical_softness_ev','S'),('electronegativity_ev','chi'),('chemical_potential_ev','mu'),('electrophilicity_index_ev','omega')]:
        v=d.get(k)
        if v is not None:
            unit=' eV^-1' if k=='chemical_softness_ev' else ' eV'
            extra.append(f'{label}: {float(v):.6f}{unit}')
    dip=(a.get('dipole') or {}).get('magnitude_debye') if isinstance(a.get('dipole'),dict) else None
    if dip is not None: extra.append(f'Dipole moment: {float(dip):.6f} D')
    if d: extra.append('I, A and related descriptors are frontier-orbital estimates, not Delta-SCF ionization/attachment energies.')
    return base + ('\n'+'\n'.join(extra) if extra else '')

_section_orbitals_v57=section_orbitals
def section_orbitals(a):
    txt=_section_orbitals_v57(a)
    d=a.get('conceptual_dft',{}) or {}
    if not d: return txt
    rows=['','Conceptual DFT descriptors:']
    mapping=[('ionization_potential_ev','Ionization potential I','eV'),('electron_affinity_ev','Electron affinity A','eV'),('chemical_hardness_ev','Chemical hardness eta','eV'),('chemical_potential_ev','Chemical potential mu','eV'),('electronegativity_ev','Electronegativity chi','eV'),('chemical_softness_ev','Chemical softness S','eV^-1'),('electrophilicity_index_ev','Electrophilicity index omega','eV'),('electrodonating_power_ev','Electrodonating power omega-','eV'),('electroaccepting_power_ev','Electroaccepting power omega+','eV'),('net_electrophilicity_ev','Net electrophilicity Delta omega','eV')]
    for k,label,unit in mapping:
        if d.get(k) is not None: rows.append(f'{label}: {float(d[k]):.8f} {unit}')
    return txt+'\n'+'\n'.join(rows)

_make_plots_v57=make_plots
def make_plots(a,outdir):
    made=_make_plots_v57(a,outdir)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    o=(a.get('orbitals') or {}).get('orbitals',[]) or []
    cleaned=[z for z in o if z.get('ev') is not None]
    occ=[z for z in cleaned if (z.get('occ') or 0.0)>1e-8]
    vir=[z for z in cleaned if (z.get('occ') or 0.0)<=1e-8]
    if not occ or not vir: return made
    # Publication-style plot: emphasize only HOMO and LUMO labels to avoid overlap.
    show_occ=occ[-3:]; show_vir=vir[:3]; homo=occ[-1]; lumo=vir[0]
    p=os.path.join(outdir,'orbital_energies.png')
    fig,ax=plt.subplots(figsize=(7.2,6.0))
    for z in show_occ[:-1]:
        ax.hlines(float(z['ev']),-0.34,-0.12,lw=1.2)
    ax.hlines(float(homo['ev']),-0.34,-0.12,lw=2.5)
    for z in show_vir[1:]:
        ax.hlines(float(z['ev']),0.12,0.34,lw=1.2)
    ax.hlines(float(lumo['ev']),0.12,0.34,lw=2.5)

    yh=float(homo['ev']); yl=float(lumo['ev']); gap=yl-yh
    ax.annotate('HOMO', xy=(-0.34,yh), xytext=(-0.50,yh), ha='right', va='center', fontsize=9.0,
                arrowprops=dict(arrowstyle='-', lw=.55, shrinkA=0, shrinkB=0))
    ax.text(-0.50, yh-0.03*max(1.0,abs(yh)), f'{yh:.2f} eV', ha='right', va='top', fontsize=8.0)
    ax.annotate('LUMO', xy=(0.34,yl), xytext=(0.50,yl), ha='left', va='center', fontsize=9.0,
                arrowprops=dict(arrowstyle='-', lw=.55, shrinkA=0, shrinkB=0))
    ax.text(0.50, yl-0.03*max(1.0,abs(yl)), f'{yl:.2f} eV', ha='left', va='top', fontsize=8.0)

    if len(show_occ) >= 2:
        ax.text(-0.50, float(show_occ[-2]['ev']), 'HOMO-1', ha='right', va='center', fontsize=7.3, alpha=0.85)
    if len(show_vir) >= 2:
        ax.text(0.50, float(show_vir[1]['ev']), 'LUMO+1', ha='left', va='center', fontsize=7.3, alpha=0.85)

    ax.annotate('',xy=(0.0,yl),xytext=(0.0,yh),arrowprops=dict(arrowstyle='<->',lw=1.1))
    ax.text(0.03,(yh+yl)/2.0,f'gap = {gap:.2f} eV',va='center',ha='left',fontsize=8.5)
    ys=[float(z['ev']) for z in show_occ+show_vir]
    ymin,ymax=min(ys),max(ys); pad=max(.75,.18*(ymax-ymin if ymax>ymin else 1.0))
    ax.set_xlim(-0.78,0.78); ax.set_ylim(ymin-pad,ymax+pad)
    ax.set_xticks([-0.21,0.21]); ax.set_xticklabels(['Occupied','Virtual'])
    ax.set_ylabel('Orbital energy (eV)'); ax.set_title('Frontier molecular orbital energy levels')
    ax.grid(axis='y',alpha=.12)
    fig.tight_layout(); fig.savefig(p,dpi=600,bbox_inches='tight'); plt.close(fig)
    made['orbitals']=p
    return made

'''

# v5.9 Psi4 parser/report hardening
ANALYZER_V59_PATCH_CODE = r'''
def _psi4_last_match(text, patterns):
    for rx in patterns:
        ms=list(re.finditer(rx,text,re.I|re.M))
        if ms:
            return ms[-1]
    return None

def _psi4_method_basis_v59(text):
    method=None; basis=None; reference=None
    for rx in [
        r'(?m)^\s*DFT Functional\s*[:=]\s*([^\n]+)',
        r'(?m)^\s*Functional\s*[:=]\s*([^\n]+)',
        r'(?m)^\s*DFT Potential\s*[:=]\s*([^\n]+)',
        r'(?m)^\s*Method\s*[:=]\s*([A-Za-z0-9+*()_\-]+)',
    ]:
        m=_psi4_last_match(text,[rx])
        if m:
            val=m.group(1).strip().split()[0]
            if val and len(val)<40:
                method=val; break
    if not method:
        m=_psi4_last_match(text,[
            r'@(?P<m>(?:DF-)?(?:RHF|ROHF|UHF|RKS|UKS|SCF|MP2|MP3|CCSD(?:\(T\))?))\s+Final Energy:',
            r'(?P<m>CCSD(?:\(T\))?|MP2|MP3)\s+(?:total\s+)?energy\s*[:=]'
        ])
        if m:
            method=m.groupdict().get('m') or m.group(1)
    m=_psi4_last_match(text,[
        r'(?m)^\s*Reference\s*[:=]\s*([A-Za-z0-9_\-]+)',
        r'(?m)^\s*SCF Type\s*[:=]\s*([A-Za-z0-9_\-]+)'
    ])
    if m: reference=m.group(1).strip()
    for rx in [
        r'(?m)^\s*BASIS\s*[:=]\s*([^\s#]+)',
        r'(?m)^\s*Basis Set\s*[:=]\s*([^\n]+)',
        r'(?m)^\s*basis\s+([A-Za-z0-9+*()_\-]+)'
    ]:
        m=_psi4_last_match(text,[rx])
        if m:
            basis=m.group(1).strip().strip('"\'')
            if len(basis)>80: basis=basis.split()[0]
            break
    return method,basis,reference

def _psi4_orbitals_v59(text):
    groups=[]; current_occ=None; current_spin='restricted'; active=False
    num_re=r'[-+]?\d*\.\d+(?:[Ee][-+]?\d+)?|[-+]?\d+(?:[Ee][-+]?\d+)?'
    int_re=r'[-+]?\d+'

    def extract_vals(chunk):
        tokens=re.findall(num_re, chunk)
        if not tokens:
            return []
        vals=[]
        if len(tokens) >= 2 and len(tokens) % 2 == 0:
            pairs=[tokens[i:i+2] for i in range(0,len(tokens),2)]
            pair_mode=sum(1 for a,b in pairs if re.fullmatch(int_re,a) and ('.' in b or 'E' in b.upper()))
            if pair_mode >= max(1, len(pairs)//2):
                vals=[_float(b) for a,b in pairs]
            else:
                vals=[_float(x) for x in tokens if ('.' in x or 'E' in x.upper())]
        else:
            vals=[_float(x) for x in tokens if ('.' in x or 'E' in x.upper())]
        vals=[v for v in vals if v is not None and -100.0 <= float(v) <= 20.0]
        return vals

    for raw in text.splitlines():
        line=raw.rstrip(); u=line.upper()
        if 'ORBITAL ENERGIES' in u:
            # Geometry iterations can print several tables. Frontiers must
            # come from the final table, never a mixture of earlier steps.
            groups=[]; active=True; current_occ=None; current_spin='restricted'; continue
        if not active:
            continue
        if groups and current_occ == 0.0 and not line.strip():
            active=False; current_occ=None; continue
        if ('ALPHA' in u and 'ORBITAL' in u): current_spin='alpha'
        elif ('BETA' in u and 'ORBITAL' in u): current_spin='beta'
        m=re.search(r'(DOUBLY\s+OCCUPIED|SINGLY\s+OCCUPIED|OCCUPIED|VIRTUAL)\s*:\s*(.*)$',line,re.I)
        if m:
            label=m.group(1).upper()
            current_occ=0.0 if 'VIRTUAL' in label else (1.0 if 'SINGLY' in label else 2.0)
            for v in extract_vals(m.group(2)):
                groups.append((current_spin,current_occ,v))
            continue
        if current_occ is not None and line.strip() and not re.search(r'[A-Za-z]{3,}',line):
            for v in extract_vals(line):
                groups.append((current_spin,current_occ,v))
    if not groups:
        return parse_orbitals(text,'Psi4')

    orbitals=[]; spin_counts={}
    for spin,occ,eh in groups:
        spin_counts[spin]=spin_counts.get(spin,0)+1
        orbitals.append({'index':spin_counts[spin],'spin':spin,'occ':occ,'eh':eh,'ev':eh*HARTREE_TO_EV})

    # Sanity cleanup: Psi4 occupied orbital energies for ordinary molecular calculations
    # should not appear as large positive Hartree values. If such values coexist with
    # normal negative occupied energies, they are almost certainly parsed indices.
    occ_all=[o for o in orbitals if (o.get('occ') or 0)>1e-8]
    if occ_all and any((o.get('eh') or 0) < 0 for o in occ_all) and any((o.get('eh') or 0) > 0.2 for o in occ_all):
        orbitals=[o for o in orbitals if not ((o.get('occ') or 0)>1e-8 and (o.get('eh') or 0) > 0.2)]

    # Remove obvious parser artefacts: occupied orbitals should not remain as large
    # positive Hartree values for ordinary neutral molecular calculations. Also drop any
    # row whose eV is exactly Eh*27.2114 when Eh is an integer-like large positive value,
    # because that pattern indicates an orbital index leaked into the energy column.
    filtered=[]
    for o in orbitals:
        eh=o.get('eh'); ev=o.get('ev'); occ=o.get('occ') or 0.0
        if eh is None:
            continue
        if occ > 1e-8 and eh > 0.5:
            continue
        if occ > 1e-8 and eh > 1.0 and abs(eh-round(eh)) < 1e-6 and ev is not None and abs(ev - eh*HARTREE_TO_EV) < 1e-3:
            continue
        filtered.append(o)
    orbitals = filtered

    occs=[x for x in orbitals if (x.get('occ') or 0)>1e-8]
    virs=[x for x in orbitals if (x.get('occ') or 0)<=1e-8]
    out={'orbitals':orbitals[-1000:]}
    if occs:
        out['homo_ev']=max(occs,key=lambda z:z['ev'])['ev']
    if virs:
        out['lumo_ev']=min(virs,key=lambda z:z['ev'])['ev']
    if out.get('homo_ev') is not None and out.get('lumo_ev') is not None:
        out['gap_ev']=out['lumo_ev']-out['homo_ev']

    # Extra sanity fallback for malformed sections.
    if out.get('homo_ev') is not None and out['homo_ev'] > 2.0:
        neg_occs=[x for x in occs if (x.get('eh') or 0) < 0.0]
        if neg_occs:
            out['homo_ev']=max(neg_occs,key=lambda z:z['ev'])['ev']
            if out.get('lumo_ev') is not None:
                out['gap_ev']=out['lumo_ev']-out['homo_ev']

    for spin in ('alpha','beta'):
        so=[x for x in orbitals if x.get('spin')==spin and (x.get('occ') or 0)>1e-8]
        sv=[x for x in orbitals if x.get('spin')==spin and (x.get('occ') or 0)<=1e-8]
        if so: out[f'{spin}_homo_ev']=max(so,key=lambda z:z['ev'])['ev']
        if sv: out[f'{spin}_lumo_ev']=min(sv,key=lambda z:z['ev'])['ev']
    return out

def _psi4_dipole_v59(text):
    d=parse_dipole(text)
    m=_psi4_last_match(text,[
        r'X\s*[:=]\s*('+FLOAT_RE+r').*?Y\s*[:=]\s*('+FLOAT_RE+r').*?Z\s*[:=]\s*('+FLOAT_RE+r').*?(?:TOTAL|MAGNITUDE)\s*[:=]\s*('+FLOAT_RE+r')',
        r'Dipole Moment.*?\n\s*X\s+Y\s+Z\s+Total\s*\n\s*('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')'
    ])
    if m:
        vals=[_float(m.group(i)) for i in range(1,5)]
        d['vector']=vals[:3]; d['magnitude_debye']=vals[3]
    return d

def _psi4_charges_v59(text):
    charges=[]; active=False
    for raw in text.splitlines():
        u=raw.upper()
        if 'MULLIKEN CHARGES' in u or 'MULLIKEN ATOMIC CHARGES' in u:
            active=True; charges=[]; continue
        if active:
            m=re.match(r'^\s*(\d+)\s+([A-Za-z]{1,3})\s+(.*)$',raw)
            if m:
                vals=[_float(x) for x in re.findall(FLOAT_RE,m.group(3))]
                vals=[v for v in vals if v is not None]
                if vals:
                    charges.append({'index':int(m.group(1)),'element':m.group(2),'charge':vals[-1]})
            elif charges and (not raw.strip() or 'LOEWDIN' in u or 'DIPOLE' in u):
                break
    return charges[-2000:]

def _psi4_thermo_v59(text, electronic_energy=None):
    t={}
    def last(patterns): return _last_number(text,patterns)
    temp=last([r'Temperature\s*[:=]\s*('+FLOAT_RE+r')\s*(?:\[?K\]?)',r'at\s*('+FLOAT_RE+r')\s*\[K\]'])
    if temp is not None: t['temperature_K']=temp
    p_atm=last([r'Pressure\s*[:=]\s*('+FLOAT_RE+r')\s*(?:atm|\[atm\])'])
    if p_atm is None:
        p_pa=last([r'Pressure\s*[:=]\s*('+FLOAT_RE+r')\s*(?:Pa|\[Pa\])'])
        if p_pa is not None: p_atm=p_pa/101325.0
    if p_atm is not None: t['pressure_atm']=p_atm
    zpe=last([r'Zero[- ]point(?: vibrational)? energy\s*[:=]?\s*('+FLOAT_RE+r')\s*\[?Eh\]?',r'ZPE(?:_vib)?\s*[:=]\s*('+FLOAT_RE+r')\s*(?:Eh|Hartree)'])
    if zpe is not None: t['zpe_hartree']=zpe
    patterns={
      'energy_0K_hartree':[r'Energy \(0 K\)\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s*$',r'Total E,?\s*(?:Electronic energy)?\s*at\s*0(?:\.0+)?\s*\[K\]\s*('+FLOAT_RE+r')\s*\[Eh\]'],
      'internal_energy_hartree':[r'Internal energy\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s*$',r'Total E,?\s*Internal energy.*?('+FLOAT_RE+r')\s*\[Eh\]'],
      'enthalpy_hartree':[r'Enthalpy\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s*$',r'Total H,?\s*Enthalpy at\s*'+FLOAT_RE+r'\s*\[K\]\s*('+FLOAT_RE+r')\s*\[Eh\]'],
      'gibbs_hartree':[r'Gibbs Free Energy\s+('+FLOAT_RE+r')\s+('+FLOAT_RE+r')\s*$',r'Total G,?\s*Free enthalpy at\s*'+FLOAT_RE+r'\s*\[K\]\s*('+FLOAT_RE+r')\s*\[Eh\]'],
    }
    for key,arr in patterns.items():
        for rx in arr:
            ms=list(re.finditer(rx,text,re.I|re.M))
            if ms:
                vals=[_float(x) for x in ms[-1].groups() if x is not None]; vals=[x for x in vals if x is not None]
                if vals: t[key]=vals[-1]
                break
    corr_patterns={
      'thermal_energy_correction_hartree':[r'Thermal correction to (?:Energy|E)\s*[:=]\s*('+FLOAT_RE+r')'],
      'enthalpy_correction_hartree':[r'Thermal correction to (?:Enthalpy|H)\s*[:=]\s*('+FLOAT_RE+r')'],
      'gibbs_correction_hartree':[r'Thermal correction to (?:Gibbs Free Energy|G)\s*[:=]\s*('+FLOAT_RE+r')'],
    }
    for key,arr in corr_patterns.items():
        v=last(arr)
        if v is not None: t[key]=v
    if electronic_energy is not None:
        if t.get('energy_0K_hartree') is None and zpe is not None: t['energy_0K_hartree']=electronic_energy+zpe
        if t.get('internal_energy_hartree') is None and t.get('thermal_energy_correction_hartree') is not None: t['internal_energy_hartree']=electronic_energy+t['thermal_energy_correction_hartree']
        if t.get('enthalpy_hartree') is None and t.get('enthalpy_correction_hartree') is not None: t['enthalpy_hartree']=electronic_energy+t['enthalpy_correction_hartree']
        if t.get('gibbs_hartree') is None and t.get('gibbs_correction_hartree') is not None: t['gibbs_hartree']=electronic_energy+t['gibbs_correction_hartree']
    T=t.get('temperature_K'); H=t.get('enthalpy_hartree'); G=t.get('gibbs_hartree')
    if T and H is not None and G is not None and T>0: t['entropy_J_mol_K']=(H-G)*HARTREE_TO_KJMOL*1000.0/T
    for k in list(t):
        if k.endswith('_hartree'): t[k.replace('_hartree','_kj_mol')]=t[k]*HARTREE_TO_KJMOL
    return t

def _psi4_final_energy_v64(text):
    """Select the last method-specific electronic energy in output order.

    An SCF ``Final Energy`` line is also printed before correlated methods.
    Taking the last *regex pattern* rather than the last calculation silently
    replaces MP2/CCSD(T) results with the SCF reference energy.
    """
    f=FLOAT_RE
    candidates=[]
    for label,rx in (
        ('CCSD(T)',r'(?m)^\s*CCSD\(T\)\s+(?:total\s+)?energy\s*[:=]\s*('+f+r')'),
        ('CCSD',r'(?m)^\s*CCSD\s+(?:total\s+)?energy\s*[:=]\s*('+f+r')'),
        ('MP3',r'(?m)^\s*MP3\s+(?:total\s+)?energy\s*[:=]\s*('+f+r')'),
        ('MP2',r'(?m)^\s*MP2\s+(?:total\s+)?energy\s*[:=]\s*('+f+r')'),
        ('SCF',r'(?m)^\s*@(?:DF-)?(?:RHF|ROHF|UHF|RKS|UKS|SCF)\s+Final Energy\s*[:=]\s*('+f+r')'),
    ):
        for match in re.finditer(rx,text,re.I):
            energy=_float(match.group(1))
            if energy is not None and math.isfinite(energy):
                candidates.append((match.start(),energy,label))
    if candidates:
        _,energy,label=max(candidates,key=lambda item:item[0])
        return energy,label
    for rx in (r'(?m)^\s*Current Energy\s*[:=]\s*('+f+r')',
               r'(?m)^\s*Final Energy\s*[:=]\s*('+f+r')'):
        matches=list(re.finditer(rx,text,re.I))
        if matches:
            energy=_float(matches[-1].group(1))
            if energy is not None and math.isfinite(energy):
                return energy,None
    return None,None

def _parse_psi4_precise_v59(text, filename):
    F=FLOAT_RE
    version=None
    for rx in [r'Psi4\s+([0-9][\w.\-]+)',r'Psi4\s+Version\s*[:=]?\s*([0-9][\w.\-]+)']:
        m=re.search(rx,text,re.I)
        if m: version=m.group(1); break
    success_marks=list(re.finditer(r'Psi4\s+exiting successfully',text,re.I))
    fatal_pattern=r'(PSIEXCEPTION|TRACEBACK|FATAL ERROR|SEGMENTATION FAULT|OUT OF MEMORY|CONVERGENCE FAILURE)'
    fatal_marks=list(re.finditer(fatal_pattern,text,re.I))
    normal=bool(success_marks and (not fatal_marks or success_marks[-1].start()>fatal_marks[-1].start()))
    fatal=[ln.strip() for ln in text.splitlines() if re.search(fatal_pattern,ln,re.I)]
    method,basis,reference=_psi4_method_basis_v59(text)
    energies={}
    final,energy_method=_psi4_final_energy_v64(text)
    if energy_method in ('CCSD(T)','CCSD','MP3','MP2'):
        method=energy_method
    if final is not None:
        energies.update(final_energy_hartree=final,final_energy_kj_mol=final*HARTREE_TO_KJMOL,final_energy_ev=final*HARTREE_TO_EV)
    for key,arr in {
        'reference_energy_hartree':[r'Reference Energy\s*[:=]\s*('+F+r')',r'SCF total energy\s*[:=]\s*('+F+r')'],
        'correlation_energy_hartree':[r'Correlation Energy\s*[:=]\s*('+F+r')',r'(?:MP2|CCSD) correlation energy\s*[:=]\s*('+F+r')'],
        'nuclear_repulsion_hartree':[r'Nuclear Repulsion Energy\s*[:=]\s*('+F+r')']}.items():
        v=_last_number(text,arr)
        if v is not None: energies[key]=v
    opt=[]
    for m in re.finditer(r'@(?:DF-)?(?:RHF|ROHF|UHF|RKS|UKS|SCF)\s+Final Energy:\s*('+F+r')',text,re.I):
        v=_float(m.group(1))
        if v is not None and (not opt or abs(v-opt[-1])>1e-12): opt.append(v)
    freqs,ir=_parse_psi4_vibrations(text); td=_parse_psi4_tddft(text)
    # A correlated final energy and an earlier SCF frequency correction need
    # an explicit composite protocol. Do not combine them silently.
    tr=_psi4_thermo_v59(text,final if energy_method=='SCF' else None)
    blocks=[]; cur=[]; active=False
    for line in text.splitlines():
        if re.search(r'(?:Final optimized geometry|Geometry \(in Angstrom\)|Cartesian Geometry \(in Angstrom\))',line,re.I):
            if cur: blocks.append(cur)
            cur=[]; active=True; continue
        if active:
            m=re.match(r'^\s*(?:\d+\s+)?([A-Za-z]{1,3})\s+('+F+r')\s+('+F+r')\s+('+F+r')\s*$',line)
            if m: cur.append({'element':m.group(1),'x':_float(m.group(2)),'y':_float(m.group(3)),'z':_float(m.group(4))}); continue
            if cur and not line.strip(): blocks.append(cur); cur=[]; active=False
    if cur: blocks.append(cur)
    geometry=blocks[-1] if blocks else []
    imag=[x for x in freqs if x < -5.0]
    if not normal and fatal: sp='FAILED_CALCULATION'; rel='FAILED_CALCULATION'
    elif not normal: sp='UNCONFIRMED_CALCULATION'; rel='UNCONFIRMED_CALCULATION'
    elif not freqs: sp='NO_FREQUENCY_CALCULATION'; rel='ELECTRONIC_ONLY'
    elif len(imag)==0: sp='LIKELY_MINIMUM'; rel='HIGH'
    elif len(imag)==1: sp='TRANSITION_STATE'; rel='TRANSITION_STATE'
    else: sp='HIGHER_ORDER_SADDLE'; rel='UNRELIABLE_FOR_MINIMUM'
    out={'filename':filename,'engine':'Psi4','version':version,'normal_termination':normal,'errors':fatal,'method':method,'basis':basis,'energies':energies,'optimization_energies':opt[-500:],'thermochemistry':tr,'frequencies_cm1':freqs,'imaginary_frequencies_cm1':imag,'stationary_point_status':sp,'thermochemistry_reliability':rel,'ir_spectrum':ir,'tddft_states':td,'orbitals':_psi4_orbitals_v59(text),'dipole':_psi4_dipole_v59(text),'atomic_charges':_psi4_charges_v59(text),'final_geometry':geometry,'raman_spectrum':[],'system':parse_charge_mult(text),'timings':parse_timings(text,'Psi4'),'psi4_details':{'reference':reference}}
    return _normalize_analysis_schema(out)

_parse_output_text_v58_for_v59=parse_output_text
def parse_output_text(text,filename='output.out'):
    if detect_engine(text)=='Psi4':
        a=_parse_psi4_precise_v59(text,filename); a['conceptual_dft']=conceptual_dft_descriptors(a); return a
    return _parse_output_text_v58_for_v59(text,filename)

_build_pdf_v58=build_pdf
def _build_psi4_pdf_v59(a, plots, pdf_path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether, LongTable, HRFlowable
    NAVY=colors.HexColor('#17324D'); SLATE=colors.HexColor('#4B5D6B'); LIGHT=colors.HexColor('#F3F6F8'); MID=colors.HexColor('#D9E1E7'); GREEN=colors.HexColor('#1F7A4D'); RED=colors.HexColor('#A33A32'); TEXT=colors.HexColor('#202830')
    styles=getSampleStyleSheet()
    title=ParagraphStyle('P9Title',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=18.5,leading=22,textColor=NAVY,alignment=TA_LEFT,spaceAfter=4)
    sub=ParagraphStyle('P9Sub',parent=styles['BodyText'],fontSize=8.3,leading=10.5,textColor=SLATE,spaceAfter=8)
    h1=ParagraphStyle('P9H1',parent=styles['Heading2'],fontName='Helvetica-Bold',fontSize=12,leading=14.5,textColor=NAVY,spaceBefore=9,spaceAfter=4)
    h2=ParagraphStyle('P9H2',parent=styles['Heading3'],fontName='Helvetica-Bold',fontSize=9.6,leading=11.5,textColor=SLATE,spaceBefore=5,spaceAfter=3)
    body=ParagraphStyle('P9Body',parent=styles['BodyText'],fontSize=8.4,leading=10.8,textColor=TEXT)
    small=ParagraphStyle('P9Small',parent=body,fontSize=7.2,leading=9.1,textColor=SLATE)
    cap=ParagraphStyle('P9Cap',parent=small,alignment=TA_CENTER,spaceBefore=3,spaceAfter=6)
    mono=ParagraphStyle('P9Mono',parent=body,fontName='Courier',fontSize=6.7,leading=8.2)
    filename=str(a.get('filename') or 'psi4.out'); version=str(a.get('version') or 'N/A'); normal=bool(a.get('normal_termination'))
    doc=SimpleDocTemplate(pdf_path,pagesize=A4,rightMargin=1.4*cm,leftMargin=1.4*cm,topMargin=1.42*cm,bottomMargin=1.42*cm,title='Psi4 Computational Chemistry Analysis Report',author='ChemBot')
    def esc(x): return str(x if x is not None else '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    def cell(x,sty=body): return Paragraph(esc(x),sty)
    def sec(txt): return [Spacer(1,3),Paragraph(txt,h1),HRFlowable(width='100%',thickness=.55,color=MID,spaceAfter=5)]
    def table(rows,widths=None,header=True):
        hh=ParagraphStyle('P9TH',parent=small,fontName='Helvetica-Bold',textColor=colors.white,fontSize=7.3,leading=8.7)
        data=[[cell(v,hh if header and r==0 else small) for v in row] for r,row in enumerate(rows)]
        t=LongTable(data,colWidths=widths,repeatRows=1 if header else 0,hAlign='LEFT')
        cmds=[('VALIGN',(0,0),(-1,-1),'MIDDLE'),('GRID',(0,0),(-1,-1),.25,MID),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),3.4),('BOTTOMPADDING',(0,0),(-1,-1),3.4)]
        if header: cmds += [('BACKGROUND',(0,0),(-1,0),NAVY),('TEXTCOLOR',(0,0),(-1,0),colors.white)]
        first=1 if header else 0
        for r in range(first,len(rows)):
            if (r-first)%2: cmds.append(('BACKGROUND',(0,r),(-1,r),LIGHT))
        t.setStyle(TableStyle(cmds)); return t
    def fig(story,key,label,note=None,maxh=9.6*cm):
        p=plots.get(key)
        if not p or not os.path.exists(p): return
        im=Image(p); scale=min(17*cm/im.imageWidth,maxh/im.imageHeight); im.drawWidth*=scale; im.drawHeight*=scale
        story.append(KeepTogether([Spacer(1,5),im,Paragraph(label+((' '+note) if note else ''),cap)]))
    def frame(canvas,d):
        canvas.saveState(); w,h=A4; canvas.setStrokeColor(MID); canvas.setLineWidth(.35); canvas.line(d.leftMargin,h-.77*cm,w-d.rightMargin,h-.77*cm); canvas.line(d.leftMargin,.70*cm,w-d.rightMargin,.70*cm); canvas.setFont('Helvetica',7); canvas.setFillColor(SLATE); canvas.drawString(d.leftMargin,h-.59*cm,'ChemBot - Psi4 analysis'); canvas.drawRightString(w-d.rightMargin,h-.59*cm,f'Psi4 {version}'[:50]); canvas.drawString(d.leftMargin,.43*cm,filename[:70]); canvas.drawRightString(w-d.rightMargin,.43*cm,f'Page {d.page}'); canvas.restoreState()
    e=a.get('energies',{}) or {}; t=a.get('thermochemistry',{}) or {}; o=a.get('orbitals',{}) or {}; d=a.get('dipole',{}) or {}; sysi=a.get('system',{}) or {}; det=a.get('psi4_details',{}) or {}
    story=[Paragraph('Psi4 Computational Chemistry Analysis Report',title),Paragraph(f'<b>File:</b> {esc(filename)} &nbsp;&nbsp; | &nbsp;&nbsp; <b>Report engine:</b> ChemBot {REPORT_GENERATOR_VERSION}',sub)]
    st='NORMAL TERMINATION' if normal else 'TERMINATION NOT CONFIRMED'; scol=GREEN if normal else RED
    bar=Table([[Paragraph(f'<b>{st}</b>',ParagraphStyle('P9Status',parent=body,fontName='Helvetica-Bold',fontSize=9,textColor=colors.white,alignment=TA_CENTER))]],colWidths=[17*cm],rowHeights=[.68*cm]); bar.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),scol),('VALIGN',(0,0),(-1,-1),'MIDDLE')])); story += [bar,Spacer(1,8)]
    meta=[['Engine','Psi4','Version',version],['Method',a.get('method') or 'N/A','Basis',a.get('basis') or 'N/A'],['Reference',det.get('reference') or 'N/A','Charge',sysi.get('charge','N/A')],['Multiplicity',sysi.get('multiplicity','N/A'),'Atoms',len(a.get('final_geometry',[]) or []) or 'N/A']]
    story += [table(meta,[2.5*cm,6*cm,2.5*cm,6*cm],header=False)]
    story += sec('Key results')
    rows=[['Quantity','Value']]
    for label,val in [('Final electronic energy',_report_fmt(e.get('final_energy_hartree'),10,' Eh')),('Gibbs free energy',_report_fmt(t.get('gibbs_hartree'),10,' Eh')),('Enthalpy',_report_fmt(t.get('enthalpy_hartree'),10,' Eh')),('HOMO',_report_fmt(o.get('homo_ev'),5,' eV')),('LUMO',_report_fmt(o.get('lumo_ev'),5,' eV')),('HOMO-LUMO gap',_report_fmt(o.get('gap_ev'),5,' eV')),('Dipole magnitude',_report_fmt(d.get('magnitude_debye'),5,' D')),('Runtime',_format_runtime((a.get('timings',{}) or {}).get('wall_seconds')) )]:
        if not str(val).startswith('N/A'): rows.append([label,val])
    story += [table(rows,[9*cm,8*cm])]
    story += sec('Electronic structure and energy decomposition')
    er=[['Quantity','Value','Unit']]
    for k,lbl in [('final_energy_hartree','Final electronic energy'),('reference_energy_hartree','Reference / SCF energy'),('correlation_energy_hartree','Correlation energy'),('nuclear_repulsion_hartree','Nuclear repulsion energy')]:
        if e.get(k) is not None: er.append([lbl,_report_fmt(e[k],10),'Eh'])
    if len(er)>1: story += [table(er,[9.2*cm,4.6*cm,3.2*cm])]
    fig(story,'optimization','Figure: optimization electronic-energy profile.','Values are displayed relative to the lowest parsed optimization energy.')
    if t:
        story += sec('Thermochemistry')
        tr=[['Quantity','Value','Unit']]
        for k,lbl,unit,dig in [('temperature_K','Temperature','K',3),('pressure_atm','Pressure','atm',5),('zpe_hartree','Zero-point vibrational energy','Eh',8),('energy_0K_hartree','Electronic + ZPE energy (0 K)','Eh',10),('internal_energy_hartree','Internal energy, U','Eh',10),('enthalpy_hartree','Enthalpy, H','Eh',10),('gibbs_hartree','Gibbs free energy, G','Eh',10),('entropy_J_mol_K','Entropy, S','J mol-1 K-1',5)]:
            if t.get(k) is not None: tr.append([lbl,_report_fmt(t[k],dig),unit])
        for k,lbl in [('thermal_energy_correction_hartree','Thermal energy correction'),('enthalpy_correction_hartree','Enthalpy correction'),('gibbs_correction_hartree','Gibbs correction')]:
            if t.get(k) is not None: tr.append([lbl,_report_fmt(t[k],8),'Eh'])
        story += [table(tr,[9.2*cm,4.6*cm,3.2*cm]),Paragraph('Thermodynamic totals are reported only when present in the Psi4 output or reconstructable from an explicitly printed electronic energy plus the corresponding Psi4 correction.',small)]
    freqs=[float(x) for x in (a.get('frequencies_cm1',[]) or []) if x is not None]; imag=[x for x in freqs if x < -5.0]; nz=[x for x in freqs if abs(x)<5.0]; pos=[x for x in freqs if x>=5.0]
    if freqs or a.get('ir_spectrum'):
        story += sec('Vibrational analysis and IR spectroscopy')
        vr=[['Diagnostic','Value'],['Stationary-point classification',a.get('stationary_point_status') or 'N/A'],['Thermochemistry reliability',a.get('thermochemistry_reliability') or 'N/A'],['Frequency entries parsed',len(freqs)],['Near-zero modes (|nu| < 5 cm-1)',len(nz)],['Imaginary modes (nu < -5 cm-1)',len(imag)],['Positive modes (nu >= 5 cm-1)',len(pos)]]
        story += [table(vr,[11.3*cm,5.7*cm])]
        if imag: story += [Paragraph('<b>Imaginary frequencies:</b> '+', '.join(f'{x:.2f} cm-1' for x in imag[:20]),small)]
        clean=[]
        for q in (a.get('ir_spectrum',[]) or []):
            try:
                f=float(q[0]); inten=abs(float(q[1] or 0))
                if f>0: clean.append((f,inten))
            except Exception: pass
        if clean:
            mx=max([v for _,v in clean] or [1]); pr=[['Frequency (cm-1)','IR intensity (km mol-1)','Relative (%)']]
            for f,inten in sorted(clean,key=lambda z:z[1],reverse=True)[:15]: pr.append([f'{f:.2f}',f'{inten:.3f}',f'{100*inten/mx:.1f}' if mx else '0.0'])
            story += [Spacer(1,5),Paragraph('Strongest calculated IR bands',h2),table(pr,[5.3*cm,6.0*cm,5.7*cm])]
        fig(story,'ir','Figure: simulated FT-IR-like profile.','The curve is derived from Psi4 harmonic IR intensities; the transmittance-like scale is a visualization, not experimental percent transmittance.')
    td=a.get('tddft_states',[]) or []
    if td:
        story += sec('Electronic excitations / TD-SCF')
        rr=[['State','Energy (eV)','Wavelength (nm)','Oscillator strength']]
        for s in td[:40]: rr.append([s.get('state',''),_report_fmt(s.get('ev'),5),_report_fmt(s.get('nm'),2),_report_fmt(s.get('f'),7)])
        story += [table(rr,[2.2*cm,4.3*cm,4.7*cm,5.8*cm])]; fig(story,'uvvis','Figure: simulated UV-Vis profile from parsed Psi4 excited-state transitions.')
    if o.get('homo_ev') is not None or (o.get('orbitals') or []):
        story += sec('Frontier orbital energies')
        fr=[['Quantity','Value']]
        for lbl,k in [('HOMO','homo_ev'),('LUMO','lumo_ev'),('HOMO-LUMO orbital-energy gap','gap_ev'),('Alpha HOMO','alpha_homo_ev'),('Alpha LUMO','alpha_lumo_ev'),('Beta HOMO','beta_homo_ev'),('Beta LUMO','beta_lumo_ev')]:
            if o.get(k) is not None: fr.append([lbl,_report_fmt(o[k],6,' eV')])
        story += [table(fr,[10.8*cm,6.2*cm])]
        desc=a.get('conceptual_dft',{}) or {}
        if desc:
            cr=[['Conceptual-DFT descriptor','Value']]
            for k,lbl,unit in [('ionization_potential_ev','Ionization potential, I','eV'),('electron_affinity_ev','Electron affinity, A','eV'),('chemical_hardness_ev','Chemical hardness, eta','eV'),('chemical_potential_ev','Chemical potential, mu','eV'),('electronegativity_ev','Electronegativity, chi','eV'),('chemical_softness_ev','Chemical softness, S','eV^-1'),('electrophilicity_index_ev','Electrophilicity index, omega','eV'),('electrodonating_power_ev','Electrodonating power, omega-','eV'),('electroaccepting_power_ev','Electroaccepting power, omega+','eV'),('net_electrophilicity_ev','Net electrophilicity, Delta omega','eV')]:
                if desc.get(k) is not None: cr.append([lbl,_report_fmt(desc[k],6,' '+unit)])
            story += [Spacer(1,5),Paragraph('Conceptual DFT descriptors',h2),table(cr,[11*cm,6*cm]),Paragraph('These are frontier-orbital approximations. For unrestricted wavefunctions, inspect the alpha/beta frontier levels before interpreting a single combined gap.',small)]
        fig(story,'orbitals','Figure: frontier orbital energy-level diagram.','This is not a 3D orbital isosurface; the gap is not an electronic excitation energy.')
    charges=a.get('atomic_charges',[]) or []; geom=a.get('final_geometry',[]) or []
    if d or charges or geom:
        story += sec('Molecular properties')
        pr=[['Property','Value']]
        if d.get('magnitude_debye') is not None: pr.append(['Dipole magnitude',_report_fmt(d['magnitude_debye'],6,' D')])
        if d.get('vector') and len(d['vector'])>=3: pr.append(['Dipole vector (X, Y, Z)','; '.join(_report_fmt(v,6) for v in d['vector'][:3])+' D'])
        if charges: pr.append(['Mulliken atomic charges parsed',len(charges)])
        if geom: pr.append(['Atoms in final geometry',len(geom)])
        story += [table(pr,[10.6*cm,6.4*cm])]
    story += sec('Calculation diagnostics')
    errs=a.get('errors',[]) or []
    dr=[['Check','Result'],['Psi4 normal termination','Yes' if normal else 'No'],['Stationary point',a.get('stationary_point_status') or 'N/A'],['Thermochemistry reliability',a.get('thermochemistry_reliability') or 'N/A'],['Process return code',a.get('process_returncode','N/A')],['Detected fatal/error lines',len(errs)],['Report generator',REPORT_GENERATOR_VERSION]]
    story += [table(dr,[11.4*cm,5.6*cm])]
    if errs:
        story += [Spacer(1,4),Paragraph('Last diagnostic lines',h2)]
        for line in errs[-12:]: story.append(Paragraph(esc(line),mono))
    orbarr=o.get('orbitals',[]) or []
    if charges or geom or orbarr: story += [PageBreak()]+sec('Appendix - detailed numerical data')
    if charges:
        rr=[['Index','Atom','Mulliken charge']]+[[x.get('index',''),x.get('element',''),_report_fmt(x.get('charge'),8)] for x in charges]
        story += [Paragraph('Mulliken atomic charges',h2),table(rr,[3*cm,4*cm,10*cm])]
    if geom:
        rr=[['Atom','X','Y','Z']]+[[x.get('element',''),_report_fmt(x.get('x'),7),_report_fmt(x.get('y'),7),_report_fmt(x.get('z'),7)] for x in geom]
        story += [Spacer(1,6),Paragraph('Final Cartesian geometry (Angstrom)',h2),table(rr,[3*cm,4.65*cm,4.65*cm,4.65*cm])]
    if orbarr:
        sel=_frontier_orbital_records_v62(o,per_side=20)
        rr=[['Spin','Label','Index','Occ.','Energy (Eh)','Energy (eV)']]+[[spin,label,z.get('index',''),_report_fmt(z.get('occ'),3),_report_fmt(z.get('eh'),8),_report_fmt(z.get('ev'),6)] for spin,label,z in sel]
        story += [Spacer(1,6),Paragraph('Frontier-centered orbital energies',h2),table(rr,[2.4*cm,2.6*cm,1.5*cm,1.5*cm,4.5*cm,4.5*cm])]
    doc.build(story,onFirstPage=frame,onLaterPages=frame)
    return pdf_path

def build_pdf(a, plots, pdf_path):
    if str((a or {}).get('engine') or '').lower()=='psi4':
        return _build_psi4_pdf_v59(a,plots,pdf_path)
    return _build_pdf_v58(a,plots,pdf_path)
'''

ANALYZER_V62_PATCH_CODE = r'''
def _frontier_orbital_channels_v62(orb):
    """Return finite, energy-sorted frontier data separated by spin channel."""
    if not isinstance(orb, dict):
        orb = {'orbitals': orb if isinstance(orb, list) else []}
    raw = orb.get('orbitals', [])
    if not isinstance(raw, list):
        raw = []
    groups = {}
    explicit_spin = False
    for row in raw:
        if not isinstance(row, dict):
            continue
        occ = _float(row.get('occ'))
        eh = _float(row.get('eh'))
        ev = _float(row.get('ev'))
        if ev is None and eh is not None:
            ev = eh * HARTREE_TO_EV
        if occ is None or ev is None or not math.isfinite(occ) or not math.isfinite(ev):
            continue
        if occ < -1e-7 or occ > 2.0001 or abs(ev) > 10000:
            continue
        occ = max(0.0, occ)
        spin_raw = str(row.get('spin') or 'restricted').strip().lower()
        if spin_raw in ('alpha', 'a', 'spin-up', 'spin up', 'up') or 'alpha' in spin_raw:
            spin = 'alpha'; explicit_spin = True
        elif spin_raw in ('beta', 'b', 'spin-down', 'spin down', 'down') or 'beta' in spin_raw:
            spin = 'beta'; explicit_spin = True
        else:
            spin = 'restricted'
        item = dict(row)
        item.update({'occ': occ, 'eh': eh, 'ev': ev, 'spin': spin})
        groups.setdefault(spin, []).append(item)

    # Some parsers expose frontier values without a complete orbital table.
    for spin in ('restricted', 'alpha', 'beta'):
        if spin == 'restricted':
            frontier_keys = (('homo_ev', 1.0), ('lumo_ev', 0.0))
        else:
            frontier_keys = ((f'{spin}_homo_ev', 1.0), (f'{spin}_lumo_ev', 0.0))
        for key, occ in frontier_keys:
            ev = _float(orb.get(key))
            if spin in ('alpha', 'beta') and ev is not None:
                explicit_spin = True
            matching_occupancy = any((float(z['occ']) > 1e-8) == (occ > 1e-8) for z in groups.get(spin, []))
            if ev is not None and math.isfinite(ev) and not matching_occupancy:
                groups.setdefault(spin, []).append({'index': None, 'occ': occ, 'eh': ev/HARTREE_TO_EV, 'ev': ev, 'spin': spin, '_synthetic': True})

    if not groups:
        h = _float(orb.get('homo_ev')); l = _float(orb.get('lumo_ev'))
        if h is not None and math.isfinite(h):
            groups.setdefault('restricted', []).append({'index': None, 'occ': 1.0, 'eh': h/HARTREE_TO_EV, 'ev': h, 'spin': 'restricted', '_synthetic': True})
        if l is not None and math.isfinite(l):
            groups.setdefault('restricted', []).append({'index': None, 'occ': 0.0, 'eh': l/HARTREE_TO_EV, 'ev': l, 'spin': 'restricted', '_synthetic': True})

    # Do not merge alpha and beta MO indices into one artificial sequence.
    if explicit_spin:
        groups.pop('restricted', None)
    ordered = {}
    for spin in ('restricted', 'alpha', 'beta'):
        rows = groups.get(spin, [])
        if not rows:
            continue
        rows = sorted(rows, key=lambda z: (float(z['ev']), _float(z.get('index')) if _float(z.get('index')) is not None else -1.0))
        occupied = [z for z in rows if float(z['occ']) > 1e-8]
        virtual = [z for z in rows if float(z['occ']) <= 1e-8]
        ordered[spin] = {'all': rows, 'occupied': occupied, 'virtual': virtual,
                         'homo': occupied[-1] if occupied else None,
                         'lumo': virtual[0] if virtual else None}
    return ordered


def _frontier_orbital_records_v62(orb, per_side=6):
    """Build stable HOMO-n/LUMO+n rows, sorted by actual orbital energy."""
    try:
        per_side = max(1, min(100, int(per_side)))
    except (TypeError, ValueError):
        per_side = 6
    output = []
    for spin, channel in _frontier_orbital_channels_v62(orb).items():
        occupied = channel['occupied']; virtual = channel['virtual']
        start = max(0, len(occupied) - per_side)
        for idx in range(start, len(occupied)):
            distance = len(occupied) - 1 - idx
            label = 'HOMO' if distance == 0 else f'HOMO-{distance}'
            output.append((spin, label, occupied[idx]))
        for idx, row in enumerate(virtual[:per_side]):
            label = 'LUMO' if idx == 0 else f'LUMO+{idx}'
            output.append((spin, label, row))
    return output


def _spread_frontier_labels_v62(items, low, high, minimum_gap):
    """Move label anchors apart in data coordinates while retaining leaders."""
    if not items:
        return []
    ordered = sorted(items, key=lambda x: float(x[1]['ev']))
    positions = [float(row['ev']) for _, row in ordered]
    for i in range(1, len(positions)):
        positions[i] = max(positions[i], positions[i-1] + minimum_gap)
    if positions[-1] > high:
        shift = positions[-1] - high
        positions = [y - shift for y in positions]
    if positions[0] < low:
        shift = low - positions[0]
        positions = [y + shift for y in positions]
    return [(ordered[i][0], ordered[i][1], positions[i]) for i in range(len(ordered))]


def _plot_frontier_orbitals_v62(analysis, outdir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    orb = (analysis or {}).get('orbitals') or {}
    channels = _frontier_orbital_channels_v62(orb)
    if not channels:
        return None
    os.makedirs(outdir, exist_ok=True)
    names = list(channels)
    shown = {}
    all_energy = []
    for spin in names:
        ch = channels[spin]
        shown[spin] = {'occupied': ch['occupied'][-3:], 'virtual': ch['virtual'][:3]}
        all_energy.extend(float(z['ev']) for z in shown[spin]['occupied'] + shown[spin]['virtual'])
    if not all_energy:
        return None

    low, high = min(all_energy), max(all_energy)
    span = high - low
    distinct_energy = sorted(set(round(v, 10) for v in all_energy))
    min_spacing = min((b-a for a,b in zip(distinct_energy, distinct_energy[1:])), default=math.inf)
    energy_digits = 5 if min_spacing < 0.001 else 3
    if span < 1.8:
        center = (high + low) / 2.0
        low, high = center - 0.9, center + 0.9
        span = 1.8
    pad = max(0.24, 0.10 * span)
    ylow, yhigh = low - pad, high + pad
    # Each annotation has two text lines. Leave enough room in display space;
    # closely spaced or degenerate energies must not stack their labels.
    min_label_gap = min(1.10, max(0.82, 0.075 * span))
    max_labels = max((max(len(v['occupied']), len(v['virtual'])) for v in shown.values()), default=1)
    required_plot_span = max(1.8, ((max_labels - 1) * min_label_gap) / 0.78 + 0.18)
    if (yhigh - ylow) < required_plot_span:
        center = (yhigh + ylow) / 2.0
        ylow, yhigh = center - required_plot_span/2.0, center + required_plot_span/2.0

    fig_width = 7.8 if len(names) == 1 else 10.2
    fig, ax = plt.subplots(figsize=(fig_width, 5.8))
    occ_color = '#245A9B'
    vir_color = '#D46A2E'
    ticks = []
    ticklabels = []
    for ci, spin in enumerate(names):
        channel = channels[spin]
        selected = shown[spin]
        base = ci * 2.55
        xo, xv = base, base + 1.02
        ticks.extend([xo, xv])
        if spin == 'restricted':
            ticklabels.extend(['Occupied', 'Virtual'])
            linestyle = '-'
            channel_name = 'Restricted'
        elif spin == 'alpha':
            ticklabels.extend(['Occupied α', 'Virtual α'])
            linestyle = '-'
            channel_name = 'Alpha spin'
        else:
            ticklabels.extend(['Occupied β', 'Virtual β'])
            linestyle = '--'
            channel_name = 'Beta spin'

        occ_rows = selected['occupied']; vir_rows = selected['virtual']
        for z in occ_rows:
            energy = float(z['ev'])
            lw = 2.6 if z is channel['homo'] else 1.45
            ax.hlines(energy, xo-0.16, xo+0.16, color=occ_color, lw=lw, linestyle=linestyle, zorder=3)
        for z in vir_rows:
            energy = float(z['ev'])
            lw = 2.6 if z is channel['lumo'] else 1.45
            ax.hlines(energy, xv-0.16, xv+0.16, color=vir_color, lw=lw, linestyle=linestyle, zorder=3)

        occ_labels = []
        for distance, z in enumerate(reversed(occ_rows)):
            label = 'HOMO' if distance == 0 else f'HOMO-{distance}'
            occ_labels.append((label, z))
        occ_labels.reverse()
        vir_labels = [('LUMO' if i == 0 else f'LUMO+{i}', z) for i, z in enumerate(vir_rows)]
        label_low = ylow + 0.07*span
        label_high = yhigh - 0.10*span
        for label, z, ytext in _spread_frontier_labels_v62(occ_labels, label_low, label_high, min_label_gap):
            y = float(z['ev'])
            ax.annotate(f'{label}\n{y:.{energy_digits}f} eV', xy=(xo-0.16, y), xytext=(xo-0.25, ytext),
                        textcoords='data', ha='right', va='center', fontsize=7.1, color='#23313F',
                        arrowprops={'arrowstyle':'-', 'lw':0.55, 'color':occ_color, 'shrinkA':1.5, 'shrinkB':1.5},
                        annotation_clip=False, zorder=4)
        for label, z, ytext in _spread_frontier_labels_v62(vir_labels, label_low, label_high, min_label_gap):
            y = float(z['ev'])
            ax.annotate(f'{label}\n{y:.{energy_digits}f} eV', xy=(xv+0.16, y), xytext=(xv+0.25, ytext),
                        textcoords='data', ha='left', va='center', fontsize=7.1, color='#23313F',
                        arrowprops={'arrowstyle':'-', 'lw':0.55, 'color':vir_color, 'shrinkA':1.5, 'shrinkB':1.5},
                        annotation_clip=False, zorder=4)

        homo, lumo = channel['homo'], channel['lumo']
        if homo is not None and lumo is not None:
            yh, yl = float(homo['ev']), float(lumo['ev'])
            gap_x = (xo + xv) / 2.0
            gap_low, gap_high = min(yh, yl), max(yh, yl)
            ax.vlines(gap_x, gap_low, gap_high, color='#687583', lw=0.9, zorder=2)
            ax.hlines([yh, yl], gap_x-0.055, gap_x+0.055, color='#687583', lw=0.9, zorder=2)
            gap = yl-yh
            gap_digits = 5 if abs(gap) < 0.001 else 3
            ax.text(gap_x, yhigh - 0.035*span, f'Δε ({channel_name}) = {gap:.{gap_digits}f} eV',
                    ha='center', va='top', fontsize=7.0, color='#4B5D6B',
                    bbox={'facecolor':'white', 'edgecolor':'none', 'alpha':0.88, 'pad':1.1})

    left = -1.38
    right = (len(names)-1)*2.55 + 2.42
    ax.set_xlim(left, right)
    ax.set_ylim(ylow, yhigh)
    ax.set_xticks(ticks)
    ax.set_xticklabels(ticklabels, fontsize=8)
    ax.set_ylabel('Orbital energy (eV)')
    ax.set_title('Frontier orbital energy levels', pad=13, fontsize=12, weight='semibold')
    ax.grid(axis='y', color='#D7DEE5', linewidth=0.65, alpha=0.72)
    ax.set_axisbelow(True)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#9AA6B2')
    ax.spines['bottom'].set_color('#9AA6B2')
    ax.tick_params(axis='y', labelsize=8, colors='#34495E')
    fig.text(0.5, 0.012, 'Energy-level diagram reconstructed from output energies and occupations; 3D isosurfaces are not available from this plot.',
             ha='center', va='bottom', fontsize=6.8, color='#667684')
    fig.tight_layout(rect=(0.01, 0.045, 0.99, 0.97))
    path = os.path.join(outdir, 'orbital_energies.png')
    try:
        fig.savefig(path, dpi=400, bbox_inches='tight', facecolor='white')
    finally:
        plt.close(fig)
    return path


_make_plots_v62_base = make_plots
def make_plots(a, outdir):
    """Preserve spectrum plots and replace the legacy frontier diagram."""
    safe_analysis = dict(a or {})
    safe_analysis['orbitals'] = {'orbitals': []}
    made = _make_plots_v62_base(safe_analysis, outdir)
    orbital_path = _plot_frontier_orbitals_v62(a, outdir)
    if orbital_path:
        made['orbitals'] = orbital_path
    return made


_parse_output_v62_base = parse_output_text
def parse_output_text(text, filename='output.out'):
    """Recompute reported frontiers from validated energies, not print order."""
    analysis = _parse_output_v62_base(text, filename)
    if not isinstance(analysis, dict):
        return analysis
    orb = analysis.get('orbitals')
    if not isinstance(orb, dict):
        return analysis
    channels = _frontier_orbital_channels_v62(orb)
    if not channels:
        return analysis
    occupied = [float(ch['homo']['ev']) for ch in channels.values() if ch.get('homo') is not None]
    virtual = [float(ch['lumo']['ev']) for ch in channels.values() if ch.get('lumo') is not None]
    if occupied:
        orb['homo_ev'] = max(occupied)
    else:
        orb.pop('homo_ev', None)
    if virtual:
        orb['lumo_ev'] = min(virtual)
    else:
        orb.pop('lumo_ev', None)
    if occupied and virtual:
        orb['gap_ev'] = orb['lumo_ev'] - orb['homo_ev']
    else:
        orb.pop('gap_ev', None)
    for spin in ('alpha', 'beta'):
        channel = channels.get(spin, {})
        for suffix, field in (('homo', 'homo'), ('lumo', 'lumo')):
            key = f'{spin}_{suffix}_ev'
            item = channel.get(field)
            if item is not None:
                orb[key] = float(item['ev'])
            else:
                orb.pop(key, None)
    analysis['orbitals'] = orb
    analysis['conceptual_dft'] = conceptual_dft_descriptors(analysis)
    return analysis


def section_orbitals(a):
    """Present the same energy-sorted, spin-resolved frontier data as the plot."""
    orb = (a or {}).get('orbitals') or {}
    lines = []
    for key in ('homo_ev', 'lumo_ev', 'gap_ev'):
        value = _float(orb.get(key))
        if value is not None and math.isfinite(value):
            lines.append(f'{key}: {value:.8f} eV')
    records = _frontier_orbital_records_v62(orb, per_side=20)
    if records:
        lines.append('')
        lines.append('Spin | Label | Index | Occupancy | Energy (Eh) | Energy (eV)')
        for spin, label, row in records:
            lines.append(f"{spin} | {label} | {row.get('index','')} | {fmt(row.get('occ'),5)} | {fmt(row.get('eh'),8)} | {fmt(row.get('ev'),8)}")
        lines.append('Orbital energies do not provide 3D orbital isosurfaces.')
    descriptors = (a or {}).get('conceptual_dft') or {}
    mapping = [
        ('ionization_potential_ev','Ionization potential I','eV'),
        ('electron_affinity_ev','Electron affinity A','eV'),
        ('chemical_hardness_ev','Chemical hardness eta','eV'),
        ('chemical_potential_ev','Chemical potential mu','eV'),
        ('electronegativity_ev','Electronegativity chi','eV'),
        ('chemical_softness_ev','Chemical softness S','eV^-1'),
        ('electrophilicity_index_ev','Electrophilicity index omega','eV'),
        ('electrodonating_power_ev','Electrodonating power omega-','eV'),
        ('electroaccepting_power_ev','Electroaccepting power omega+','eV'),
        ('net_electrophilicity_ev','Net electrophilicity Delta omega','eV'),
    ]
    extra = [f'{label}: {float(descriptors[key]):.8f} {unit}' for key,label,unit in mapping if descriptors.get(key) is not None]
    if extra:
        lines.extend(['','Conceptual DFT descriptors (frontier-orbital estimates):'] + extra)
    return '\n'.join(lines) if lines else 'No valid orbital-energy table was recognized.'
'''

ANALYZER_MODULE_CODE = ANALYZER_MODULE_CODE + "\n" + ANALYZER_V41_PATCH_CODE + "\n" + ANALYZER_V58_PATCH_CODE + "\n" + ANALYZER_V59_PATCH_CODE + "\n" + ANALYZER_V62_PATCH_CODE

exec(ANALYZER_MODULE_CODE, globals())

# ------------------------- Psi4 dependency detection -------------------------
PSI4_EXTRA_RULES = [
    (re.compile(r"(?:-d3bj|-d3zero|-d3m|-d3\b|s-dftd3|dftd3)", re.I), "dftd3-python"),
    (re.compile(r"(?:-d4\b|dftd4)", re.I), "dftd4-python"),
    (re.compile(r"(?:\bgcp\b|mctc-gcp)", re.I), "gcp-correction"),
    (re.compile(r"(?:geometric|optimizer\s*=\s*['\"]?geometric)", re.I), "geometric"),
    (re.compile(r"\bqcengine\b", re.I), "qcengine"),
    (re.compile(r"\bqcelemental\b", re.I), "qcelemental"),
]


def detect_psi4_extras(inp_text):
    found = []
    for rx, pkg in PSI4_EXTRA_RULES:
        if rx.search(inp_text) and pkg not in found:
            found.append(pkg)
    return found


# ------------------------- Kaggle runner code -------------------------
KAGGLE_RUNNER_CODE = r'''
import os, sys, json, time, base64, shutil, zipfile, tarfile, glob, subprocess, traceback, urllib.request, urllib.parse, hashlib, re
from pathlib import Path
START_TIME=time.time()
HARD_LIMIT=11.5*3600
SAFETY_MARGIN=20*60

def tg(method, fields=None, file_field=None, file_path=None, timeout=90):
    """Call Telegram Bot API and fail loudly on HTTP/API-level errors."""
    fields=fields or {}
    url='https://api.telegram.org/bot'+BOT_TOKEN+'/'+method
    if file_field and file_path:
        # File uploads use requests so multipart responses can be validated.
        import requests
        with open(file_path,'rb') as fh:
            r=requests.post(url,data=fields,files={file_field:fh},timeout=timeout)
        r.raise_for_status()
        try:
            payload=r.json()
        except Exception:
            payload=None
        if isinstance(payload,dict) and not payload.get('ok',False):
            raise RuntimeError('Telegram API error: '+str(payload.get('description') or payload))
        return payload if payload is not None else r.content
    data=urllib.parse.urlencode(fields).encode('utf-8')
    raw=urllib.request.urlopen(urllib.request.Request(url,data=data),timeout=timeout).read()
    try:
        payload=json.loads(raw.decode('utf-8'))
        if isinstance(payload,dict) and not payload.get('ok',False):
            raise RuntimeError('Telegram API error: '+str(payload.get('description') or payload))
        return payload
    except UnicodeDecodeError:
        return raw

def send_msg(msg):
    for attempt in range(3):
        try:
            tg('sendMessage',{'chat_id':CHAT_ID,'text':str(msg)[:3900]})
            return True
        except Exception:
            if attempt<2:
                time.sleep(2**attempt)
    print('[Kaggle] Telegram message delivery failed after three attempts.',file=sys.stderr)
    return False

def safe_install(cmd):
    send_msg('[Kaggle] Installing: '+' '.join(cmd))
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if p.returncode!=0:
        raise RuntimeError('Dependency installation failed: '+' '.join(cmd)+'\n'+p.stdout[-2500:])

def remaining_time():
    return max(60, HARD_LIMIT-SAFETY_MARGIN-(time.time()-START_TIME))


def _windows_safe_basename(value):
    """Keep returned Gaussian files portable to Windows/GaussView."""
    name=os.path.basename(str(value).replace('\\','/'))
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).rstrip(' .')
    if not name or name in ('.','..'):
        name='gaussian_input'
    stem=os.path.splitext(name)[0].upper()
    if stem in {'CON','PRN','AUX','NUL'} or re.fullmatch(r'(COM|LPT)[1-9]',stem):
        name='_'+name
    return name


# Official Telegram Bot API accepts documents up to 50 MB. 47 MiB is kept
# below 50,000,000 bytes and leaves a practical safety margin.
TG_SAFE_FILE_BYTES=47*1024*1024
TG_SEND_RETRIES=3
TG_SEND_DELAY=1.10


def _sha256(path, chunk=4*1024*1024):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while True:
            block=f.read(chunk)
            if not block: break
            h.update(block)
    return h.hexdigest()


def _send_document_retry(path, caption='', retries=TG_SEND_RETRIES):
    """Send one document with bounded retries and Telegram flood spacing."""
    last=None
    for attempt in range(1,retries+1):
        try:
            tg('sendDocument',{'chat_id':CHAT_ID,'caption':str(caption)[:1024]},'document',path,timeout=180)
            time.sleep(TG_SEND_DELAY)
            return True
        except Exception as e:
            last=e
            if attempt<retries:
                time.sleep(min(2**attempt,8))
    send_msg('[Transfer] Failed to send '+os.path.basename(path)+': '+str(last))
    return False


def _split_file(path, chunk_bytes=None):
    """Split a too-large result into lossless Telegram-safe binary parts."""
    chunk_bytes=TG_SAFE_FILE_BYTES if chunk_bytes is None else int(chunk_bytes)
    parts=[]
    total=os.path.getsize(path)
    count=(total+chunk_bytes-1)//chunk_bytes
    with open(path,'rb') as src:
        for idx in range(1,count+1):
            part=path+'.part%03d-of-%03d' % (idx,count)
            with open(part,'wb') as dst:
                remaining=chunk_bytes
                while remaining>0:
                    block=src.read(min(4*1024*1024,remaining))
                    if not block: break
                    dst.write(block); remaining-=len(block)
            parts.append(part)
    return parts


def _write_transfer_manifest(results_dir, manifest_path):
    entries=[]
    for name in sorted(os.listdir(results_dir)):
        path=os.path.join(results_dir,name)
        if not os.path.isfile(path): continue
        entries.append({'file':name,'size_bytes':os.path.getsize(path),'sha256':_sha256(path)})
    with open(manifest_path,'w',encoding='utf-8') as f:
        f.write('ChemBot result transfer manifest\n')
        f.write('Files are listed with SHA-256 for integrity verification.\n\n')
        for x in entries:
            f.write('%s\t%d bytes\tSHA256=%s\n' % (x['file'],x['size_bytes'],x['sha256']))
        f.write('\nIf a file was sent as .partNNN-of-NNN pieces, reconstruct it in binary order.\n')
        f.write('Linux/macOS: cat filename.part* > filename\n')
        f.write('Windows CMD: copy /b filename.part001-of-NNN+filename.part002-of-NNN+... filename\n')
    return entries


def send_results_with_fallback(archive, results_dir):
    """Deliver results without silently discarding oversized ORCA/Psi4 artifacts.

    Strategy:
      1) Try the complete ZIP only when it is Telegram-safe.
      2) On oversize or any ZIP-upload failure, send result files individually.
      3) Split files larger than the safe Telegram payload into lossless parts.
      4) Send an integrity manifest containing size and SHA-256 for every original file.
    """
    archive_size=os.path.getsize(archive) if os.path.exists(archive) else 0
    if archive_size and archive_size<=TG_SAFE_FILE_BYTES:
        if _send_document_retry(archive,'Calculation files archive'):
            return {'mode':'archive','sent':1,'failed':[]}
        send_msg('[Transfer] ZIP upload failed after retries; switching to per-file rescue mode.')
    elif archive_size:
        send_msg('[Transfer] ZIP is %.1f MB and exceeds the safe Telegram upload size. Starting per-file rescue.' % (archive_size/1024/1024))

    manifest_path=os.path.join(results_dir,'CHEMBOT_TRANSFER_MANIFEST.txt')
    entries=_write_transfer_manifest(results_dir,manifest_path)
    # Scientific text/checkpoint artifacts first; very large trajectories/cubes later.
    priority={'.log':0,'.out':1,'.fchk':2,'.chk':3,'.gjf':4,'.com':4,'.property.txt':5,'.property.json':6,'.xyz':7,'.hess':8,'.gbw':9,'.molden.input':10,'.engrad':11,'.opt':12,'.trj':13,'.allxyz':14,'.cube':15,'.dat':16}
    def key_for(entry):
        name=entry['file'].lower()
        rank=50
        for ext,r in priority.items():
            if name.endswith(ext): rank=min(rank,r)
        return (rank,entry['size_bytes'],name)
    sent=0; failed=[]; split_temp=[]
    try:
        for entry in sorted(entries,key=key_for):
            path=os.path.join(results_dir,entry['file'])
            size=entry['size_bytes']
            if size<=TG_SAFE_FILE_BYTES:
                cap='Result file: %s (%.2f MB)' % (entry['file'],size/1024/1024)
                if _send_document_retry(path,cap): sent+=1
                else: failed.append(entry['file'])
                continue
            parts=_split_file(path)
            split_temp.extend(parts)
            send_msg('[Transfer] %s is %.1f MB; sending %d lossless parts.' % (entry['file'],size/1024/1024,len(parts)))
            ok=True
            for i,part in enumerate(parts,1):
                cap='%s — part %d/%d | original SHA256 %s' % (entry['file'],i,len(parts),entry['sha256'][:16]+'…')
                if _send_document_retry(part,cap): sent+=1
                else: ok=False
            if not ok: failed.append(entry['file'])
        # Manifest last so it remains easy to find after all parts.
        if _send_document_retry(manifest_path,'Transfer manifest / SHA-256 checksums'): sent+=1
        else: failed.append(os.path.basename(manifest_path))
    finally:
        for part in split_temp:
            try: os.remove(part)
            except Exception: pass
    if failed:
        send_msg('[Transfer] Rescue completed with unsent item(s): '+', '.join(failed[:30]))
    else:
        send_msg('[Transfer] Rescue completed successfully. All result bytes were delivered.')
    return {'mode':'fallback','sent':sent,'failed':failed}

GAUSSIAN_SCRATCH_DIR=None
try:
    safe_install([sys.executable,'-m','pip','install','-q','requests','matplotlib>=3.7','reportlab>=4.0','numpy'])
    send_msg('[Kaggle] Session connected. Preparing calculation...')

    files_dict=json.loads(base64.b64decode(ENCODED_FILES_JSON).decode('utf-8'))
    staged_names=set()
    for fname,b64content in files_dict.items():
        safe_name=_windows_safe_basename(fname)
        if safe_name in staged_names:
            raise RuntimeError('Two uploaded files map to the same Windows-safe filename: '+safe_name)
        staged_names.add(safe_name)
        with open(safe_name,'wb') as f: f.write(base64.b64decode(b64content))
    INPUT_FILE=_windows_safe_basename(INPUT_FILE)

    if DRIVE_LINK:
        send_msg('[Kaggle] Downloading restart archive...')
        if urllib.parse.urlsplit(DRIVE_LINK).scheme.lower()!='https':
            raise RuntimeError('Restart archive URL must use HTTPS.')
        req=urllib.request.Request(DRIVE_LINK,headers={'User-Agent':'ChemBot/4.0'})
        with urllib.request.urlopen(req,timeout=120) as r, open('restart_files.zip','wb') as f:
            if urllib.parse.urlsplit(r.geturl()).scheme.lower()!='https':
                raise RuntimeError('Restart archive redirected to a non-HTTPS URL.')
            downloaded=0
            while True:
                chunk=r.read(4*1024*1024)
                if not chunk: break
                downloaded+=len(chunk)
                if downloaded>1024*1024*1024:
                    raise RuntimeError('Restart archive exceeds 1 GiB safety limit.')
                f.write(chunk)
        with zipfile.ZipFile('restart_files.zip') as z:
            infos=z.infolist()
            if sum(max(0,info.file_size) for info in infos)>20*1024*1024*1024:
                raise RuntimeError('Restart archive expands beyond 20 GiB.')
            restart_root=os.path.abspath('.')
            for info in infos:
                target=os.path.abspath(os.path.join('.',info.filename))
                if not (target.startswith(restart_root+os.sep) or (target==restart_root and info.is_dir())):
                    raise RuntimeError('Unsafe ZIP path detected.')
            z.extractall('.')
        os.remove('restart_files.zip')

    JOB_ENGINE=str(JOB_ENGINE).strip().lower()
    if JOB_ENGINE not in ('orca','psi4','gaussian'):
        raise RuntimeError('Unsupported calculation engine: '+JOB_ENGINE)
    is_psi4=JOB_ENGINE=='psi4'
    is_gaussian=JOB_ENGINE=='gaussian'
    basename=os.path.splitext(INPUT_FILE)[0]
    output_file=basename+('.log' if is_gaussian else '.out')
    GAUSSIAN_SCRATCH_DIR=None
    if is_gaussian:
        gaussian_input=Path(INPUT_FILE).read_text(encoding='utf-8',errors='replace')
        gaussian_lines=[]
        has_checkpoint=False
        for line in gaussian_input.split('\n'):
            link0=re.match(r'^(\s*%(?:oldchk|chk)\s*=\s*)(.*?)(\s*)$',line,re.I)
            if link0:
                raw_path=link0.group(2).strip()
                if len(raw_path)>=2 and raw_path[0]==raw_path[-1] and raw_path[0] in ('"',"'"):
                    raw_path=raw_path[1:-1]
                local_name=_windows_safe_basename(raw_path.replace('\\','/').rsplit('/',1)[-1])
                quoted='"'+local_name+'"' if any(ch.isspace() for ch in local_name) else local_name
                line=link0.group(1)+quoted+link0.group(3)
                if re.match(r'^\s*%chk\s*=',line,re.I):
                    has_checkpoint=True
            gaussian_lines.append(line)
        gaussian_input='\n'.join(gaussian_lines)
        if not has_checkpoint and not re.search(r'(?im)^\s*%nosave\b',gaussian_input):
            # A checkpoint enables GaussView orbital/geometry inspection. The
            # executed input is retained in the returned result bundle.
            checkpoint_stem=re.sub(r'[^A-Za-z0-9_.-]+','_',basename).strip('._') or 'gaussian_job'
            gaussian_input='%chk='+checkpoint_stem+'.chk\n'+gaussian_input
        # GaussianView on Windows commonly writes absolute Link 0 paths (and
        # CRLF files). Persist the normalized portable input even when the user
        # already supplied %chk/%oldchk, so Gaussian never receives a Windows
        # drive path inside the Linux Kaggle runtime.
        Path(INPUT_FILE).write_text(gaussian_input,encoding='utf-8',newline='\n')
    if is_psi4:
        send_msg('[Kaggle] Preparing Psi4 environment...')

        PSI4_PREFIX='/kaggle/working/psi4_env'

        def _valid_conda(path):
            if not path or not os.path.isfile(path):
                return False
            try:
                p=subprocess.run([path,'--version'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=20)
                out=(p.stdout or '').lower()
                return p.returncode==0 and 'conda' in out
            except Exception:
                return False

        def _find_real_conda():
            # Kaggle commonly ships its real Conda under /opt/conda/bin.
            # Do NOT trust shutil.which('mamba'): /usr/local/bin/mamba can be
            # an unrelated Python testing CLI, which caused the previous failure.
            candidates=[
                '/opt/conda/bin/conda',
                '/usr/local/conda/bin/conda',
                shutil.which('conda'),
            ]
            seen=set()
            for c in candidates:
                if not c:
                    continue
                c=os.path.realpath(c)
                if c in seen:
                    continue
                seen.add(c)
                if _valid_conda(c):
                    return c
            return None

        def _install_psi4_with_conda(conda_exe):
            # Use an isolated prefix rather than altering Kaggle's base env.
            # Stable Psi4 1.11 is available for Linux/Python 3.12.
            packages=['python=3.12','psi4=1.11']
            for pkg in (PSI4_EXTRAS or []):
                if pkg not in packages:
                    packages.append(pkg)
            cmd_install=[conda_exe,'create','-y','-p',PSI4_PREFIX,'-c','conda-forge']+packages
            safe_install(cmd_install)
            return os.path.join(PSI4_PREFIX,'bin','psi4')

        def _install_psi4_standalone():
            # Official Psi4 standalone installer fallback for Linux x86_64.
            # It bundles its own Conda/Python, avoiding Kaggle base-env conflicts.
            url='https://vergil.chemistry.gatech.edu/psicode-download/Psi4conda-1.11-py312-Linux-x86_64.sh'
            installer='/kaggle/working/Psi4conda-1.11-py312-Linux-x86_64.sh'
            send_msg('[Kaggle] Conda was not available; using the official Psi4 1.11 standalone installer...')
            req=urllib.request.Request(url,headers={'User-Agent':'ChemBot/5.6'})
            with urllib.request.urlopen(req,timeout=1800) as r, open(installer,'wb') as f:
                shutil.copyfileobj(r,f)
            if not os.path.exists(installer) or os.path.getsize(installer)<1024*1024:
                raise RuntimeError('Psi4 standalone installer download failed or was unexpectedly small.')
            safe_install(['bash',installer,'-b','-p',PSI4_PREFIX])
            try:
                os.remove(installer)
            except OSError:
                pass
            conda2=os.path.join(PSI4_PREFIX,'bin','conda')
            if PSI4_EXTRAS and os.path.isfile(conda2):
                safe_install([conda2,'install','-y','-p',PSI4_PREFIX,'-c','conda-forge']+list(PSI4_EXTRAS))
            return os.path.join(PSI4_PREFIX,'bin','psi4')

        psi4_exe=shutil.which('psi4')
        if psi4_exe:
            send_msg('[Kaggle] Existing Psi4 executable found: '+psi4_exe)
        else:
            conda_exe=_find_real_conda()
            if conda_exe:
                send_msg('[Kaggle] Installing Psi4 1.11 with '+conda_exe+' ...')
                psi4_exe=_install_psi4_with_conda(conda_exe)
            else:
                psi4_exe=_install_psi4_standalone()

        if not os.path.isfile(psi4_exe):
            raise RuntimeError('Psi4 setup completed but no executable was found at '+str(psi4_exe))
        try:
            os.chmod(psi4_exe,0o755)
        except OSError:
            pass

        # Smoke-test the exact executable that will run the user's input.
        probe=subprocess.run([psi4_exe,'--version'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=60)
        if probe.returncode!=0:
            raise RuntimeError('Psi4 executable failed its version check:\n'+(probe.stdout or '')[-1500:])
        send_msg('[Kaggle] '+(probe.stdout or 'Psi4 ready').strip().splitlines()[0][:300])
        cmd=[psi4_exe,'-i',INPUT_FILE,'-o',output_file]
    elif is_gaussian:
        send_msg('[Kaggle] Preparing Gaussian 16 from attached Dataset...')
        gaussian_archives=[]
        for parent,dirs,files in os.walk('/kaggle/input'):
            for fn in files:
                if fn.lower()=='g16.tbz':
                    gaussian_archives.append(os.path.join(parent,fn))
        if not gaussian_archives:
            raise RuntimeError("Dataset 'gauusian16' is attached, but no g16.tbz archive was found under /kaggle/input.")
        gaussian_archives.sort(key=lambda p:(0 if 'gauusian16' in os.path.normpath(p).lower().split(os.sep) else 1,p))
        GAUSSIAN_RUNTIME_ROOT='/kaggle/working/gaussian16_runtime'
        shutil.rmtree(GAUSSIAN_RUNTIME_ROOT,ignore_errors=True)
        os.makedirs(GAUSSIAN_RUNTIME_ROOT,exist_ok=True)

        def _extract_gaussian_tbz(archive_path,destination):
            with tarfile.open(archive_path,'r:bz2') as archive:
                members=archive.getmembers()
                if not members:
                    raise RuntimeError('The g16.tbz archive is empty.')
                unpacked=sum(max(0,int(m.size)) for m in members if m.isfile())
                if unpacked>20*1024*1024*1024:
                    raise RuntimeError('The expanded Gaussian archive exceeds the 20 GiB safety limit.')
                root=os.path.realpath(destination)
                for member in members:
                    name=member.name.replace('\\','/')
                    normalized=os.path.normpath(name)
                    if not name or name.startswith('/') or normalized in ('.','..') or normalized.startswith('..'+os.sep):
                        raise RuntimeError('Unsafe path in g16.tbz: '+member.name)
                    target=os.path.realpath(os.path.join(destination,name))
                    if target!=root and not target.startswith(root+os.sep):
                        raise RuntimeError('Unsafe path in g16.tbz: '+member.name)
                    if member.issym() or member.islnk():
                        link=member.linkname.replace('\\','/')
                        link_base=os.path.dirname(name) if member.issym() else ''
                        link_target=os.path.realpath(os.path.join(root,link_base,link))
                        if os.path.isabs(link) or (link_target!=root and not link_target.startswith(root+os.sep)):
                            raise RuntimeError('Unsafe link in g16.tbz: '+member.name)
                    if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                        raise RuntimeError('Unsupported special file in g16.tbz: '+member.name)
                if hasattr(tarfile,'data_filter'):
                    archive.extractall(destination,filter='data')
                else:
                    archive.extractall(destination)

        _extract_gaussian_tbz(gaussian_archives[0],GAUSSIAN_RUNTIME_ROOT)
        profile_candidates=[]
        for parent,dirs,files in os.walk(GAUSSIAN_RUNTIME_ROOT):
            if os.path.basename(parent)=='bsd' and 'g16.profile' in files:
                profile_candidates.append(os.path.join(parent,'g16.profile'))
        if not profile_candidates:
            raise RuntimeError('g16.tbz was extracted, but g16/bsd/g16.profile is missing. Check that the Dataset contains the Linux binary archive.')
        profile_candidates.sort(key=lambda p:(p.count(os.sep),p))
        GAUSSIAN_PROFILE=profile_candidates[0]
        gaussian_bsd=os.path.dirname(GAUSSIAN_PROFILE)
        gaussian_home=os.path.dirname(gaussian_bsd)
        gaussian_root=os.path.dirname(gaussian_home)
        GAUSSIAN_SCRATCH_DIR='/kaggle/working/gaussian_scratch'
        os.makedirs(GAUSSIAN_SCRATCH_DIR,exist_ok=True)
        gaussian_env=os.environ.copy()
        gaussian_env['g16root']=gaussian_root
        gaussian_env['GAUSS_SCRDIR']=GAUSSIAN_SCRATCH_DIR
        gaussian_env['CHEMBOT_G16_PROFILE']=GAUSSIAN_PROFILE
        gaussian_env['CHEMBOT_GAUSS_SCRDIR']=GAUSSIAN_SCRATCH_DIR

        install_script=os.path.join(gaussian_bsd,'install')
        if os.path.isfile(install_script):
            try: os.chmod(install_script,0o755)
            except OSError: pass
            install=subprocess.run([install_script],cwd=gaussian_home,env=gaussian_env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=300)
            if install.returncode!=0:
                raise RuntimeError('Gaussian bsd/install failed:\n'+(install.stdout or '')[-2500:])
        gaussian_exe=os.path.join(gaussian_home,'g16')
        if not os.path.exists(gaussian_exe):
            raise RuntimeError('Gaussian installation did not produce g16 at '+gaussian_exe)
        try: os.chmod(gaussian_exe,0o755)
        except OSError: pass
        gaussian_env['CHEMBOT_G16_EXE']=gaussian_exe
        gaussian_formchk=os.path.join(gaussian_home,'formchk')
        gaussian_env['CHEMBOT_FORMCHK_EXE']=gaussian_formchk if os.path.isfile(gaussian_formchk) else ''
        if gaussian_env['CHEMBOT_FORMCHK_EXE']:
            try: os.chmod(gaussian_formchk,0o755)
            except OSError: pass
        # The official profile initializes Gaussian paths and runtime variables.
        GAUSSIAN_LAUNCH_SCRIPT='source "$CHEMBOT_G16_PROFILE" >/dev/null 2>&1 || exit 126; export GAUSS_SCRDIR="$CHEMBOT_GAUSS_SCRDIR"; if command -v g16 >/dev/null 2>&1; then exec g16; fi; [ -x "$CHEMBOT_G16_EXE" ] || exit 127; exec "$CHEMBOT_G16_EXE"'
        cmd=['bash','-c',GAUSSIAN_LAUNCH_SCRIPT]
        send_msg('[Kaggle] Gaussian 16 runtime prepared from '+os.path.basename(gaussian_archives[0]))
    else:
        send_msg('[Kaggle] Preparing ORCA 6 environment from attached Dataset...')
        ORCA_SCRATCH='/tmp/orca_pkg'
        os.makedirs(ORCA_SCRATCH,exist_ok=True)

        def _find_orca_under(root_dir):
            if not os.path.isdir(root_dir):
                return None
            matches=[]
            for parent,dirs,files in os.walk(root_dir):
                if 'orca' in files:
                    matches.append(os.path.join(parent,'orca'))
            if not matches:
                return None
            # Prefer a shallow path; it is normally the package root.
            matches.sort(key=lambda x:(x.count(os.sep),len(x)))
            return matches[0]

        def _extract_orca_archive(archive_path,dest):
            os.makedirs(dest,exist_ok=True)
            try:
                root=os.path.realpath(dest)
                max_expanded=20*1024*1024*1024
                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path) as z:
                        infos=z.infolist()
                        if sum(max(0,info.file_size) for info in infos)>max_expanded:
                            raise RuntimeError('ORCA ZIP expands beyond 20 GiB.')
                        for info in infos:
                            name=info.filename.replace('\\','/')
                            norm=os.path.normpath(name)
                            target=os.path.realpath(os.path.join(dest,norm))
                            if (not name or name.startswith('/') or norm=='..' or norm.startswith('..'+os.sep)
                                or not (target==root or target.startswith(root+os.sep))):
                                raise RuntimeError('Unsafe path in ORCA ZIP archive: '+name)
                        z.extractall(dest)
                    return True
                if tarfile.is_tarfile(archive_path):
                    with tarfile.open(archive_path,'r:*') as t:
                        members=t.getmembers()
                        if sum(max(0,int(m.size)) for m in members if m.isfile())>max_expanded:
                            raise RuntimeError('ORCA tar expands beyond 20 GiB.')
                        for m in members:
                            name=m.name.replace('\\','/')
                            normalized=os.path.normpath(name)
                            target=os.path.realpath(os.path.join(dest,normalized))
                            if (not name or name.startswith('/') or normalized=='..' or normalized.startswith('..'+os.sep)
                                or not (target==root or target.startswith(root+os.sep))):
                                raise RuntimeError('Unsafe path in ORCA tar archive: '+m.name)
                            if m.issym() or m.islnk():
                                link=m.linkname.replace('\\','/')
                                link_base=os.path.dirname(normalized) if m.issym() else ''
                                linked=os.path.realpath(os.path.join(root,link_base,link))
                                if os.path.isabs(link) or not (linked==root or linked.startswith(root+os.sep)):
                                    raise RuntimeError('Unsafe link in ORCA tar archive: '+m.name)
                            if not (m.isdir() or m.isfile() or m.issym() or m.islnk()):
                                raise RuntimeError('Unsupported special file in ORCA tar archive: '+m.name)
                        if hasattr(tarfile,'data_filter'):
                            t.extractall(dest,filter='data')
                        else:
                            t.extractall(dest)
                    return True
            except Exception as exc:
                send_msg('[Kaggle] ORCA archive extraction failed: '+str(exc))
            return False

        # Same strategy as chemistry-web-lab: search the read-only Kaggle Dataset
        # first; only extract archives to writable scratch if no executable exists.
        orca_exe=_find_orca_under('/kaggle/input')
        if not orca_exe:
            archives=[]
            for parent,dirs,files in os.walk('/kaggle/input'):
                for fn in files:
                    low=fn.lower()
                    if low.endswith(('.tar.xz','.txz','.tar.gz','.tgz','.tar.bz2','.tbz2','.tar','.zip')):
                        path=os.path.join(parent,fn)
                        archives.append((0 if 'orca' in low else 1,path))
            archives.sort()
            for idx,(_,arc) in enumerate(archives):
                dest=os.path.join(ORCA_SCRATCH,'extracted_%d'%idx)
                if _extract_orca_archive(arc,dest):
                    orca_exe=_find_orca_under(dest)
                    if orca_exe:
                        break
                shutil.rmtree(dest,ignore_errors=True)
        if not orca_exe:
            raise RuntimeError("Could not locate the ORCA executable in attached Dataset 'abdulsalsmsalih/orca-6-1-0'. Ensure it contains the Linux ORCA package or archive.")
        try: os.chmod(orca_exe,0o755)
        except Exception: pass
        orca_dir=os.path.dirname(os.path.realpath(orca_exe))
        os.environ['PATH']=orca_dir+os.pathsep+os.environ.get('PATH','')
        os.environ['LD_LIBRARY_PATH']=orca_dir+os.pathsep+os.environ.get('LD_LIBRARY_PATH','')
        os.environ['OMPI_ALLOW_RUN_AS_ROOT']='1'
        os.environ['OMPI_ALLOW_RUN_AS_ROOT_CONFIRM']='1'
        os.environ['OMPI_MCA_btl_vader_single_copy_mechanism']='none'
        os.environ['OMPI_MCA_rmaps_base_oversubscribe']='1'
        os.environ['OMP_NUM_THREADS']='1'
        os.environ['MKL_NUM_THREADS']='1'
        send_msg('[Kaggle] ORCA executable: '+orca_exe)
        cmd=[orca_exe,INPUT_FILE]

    engine_label='Gaussian 16' if is_gaussian else ('Psi4' if is_psi4 else 'ORCA')
    send_msg(f"[Kaggle] Launching {engine_label} calculation: {INPUT_FILE}")
    timeout_triggered=False
    try:
        if is_psi4:
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            send_msg('✅ '+engine_label+' calculation process started on Kaggle.\nRun link: '+str(KAGGLE_NOTEBOOK_URL))
            try:
                proc_stdout,_=proc.communicate(timeout=remaining_time())
            except subprocess.TimeoutExpired:
                proc.kill(); proc_stdout,_=proc.communicate(); timeout_triggered=True
                send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
            rc=proc.returncode
            if not os.path.exists(output_file) and proc_stdout:
                Path(output_file).write_text(proc_stdout,encoding='utf-8',errors='replace')
        elif is_gaussian:
            with open(INPUT_FILE,'rb') as fin, open(output_file,'wb') as fout:
                proc=subprocess.Popen(cmd,stdin=fin,stdout=fout,stderr=subprocess.STDOUT,cwd=os.getcwd(),env=gaussian_env)
                send_msg('✅ '+engine_label+' calculation process started on Kaggle.\nRun link: '+str(KAGGLE_NOTEBOOK_URL))
                try:
                    rc=proc.wait(timeout=remaining_time())
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(); rc=124; timeout_triggered=True
                    send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
        else:
            with open(output_file,'w',encoding='utf-8',errors='replace') as fout:
                proc=subprocess.Popen(cmd,stdout=fout,stderr=subprocess.STDOUT)
                send_msg('✅ '+engine_label+' calculation process started on Kaggle.\nRun link: '+str(KAGGLE_NOTEBOOK_URL))
                try:
                    rc=proc.wait(timeout=remaining_time())
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(); rc=124; timeout_triggered=True
                    send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
    except Exception as exc:
        send_msg('[Kaggle] Could not start or wait for '+engine_label+' process: '+str(exc))
        raise

    bundle=None
    if is_gaussian:
        checkpoints=[]
        runtime_real=os.path.realpath(GAUSSIAN_RUNTIME_ROOT)
        for parent,dirs,files in os.walk(os.getcwd()):
            parent_real=os.path.realpath(parent)
            if parent_real==runtime_real or parent_real.startswith(runtime_real+os.sep):
                dirs[:]=[]
                continue
            dirs[:]=[d for d in dirs if not (os.path.realpath(os.path.join(parent,d))==runtime_real or os.path.realpath(os.path.join(parent,d)).startswith(runtime_real+os.sep))]
            for fn in files:
                if fn.lower().endswith('.chk'):
                    checkpoints.append(os.path.join(parent,fn))
        converted=0
        formchk_script='source "$CHEMBOT_G16_PROFILE" >/dev/null 2>&1 || exit 126; export GAUSS_SCRDIR="$CHEMBOT_GAUSS_SCRDIR"; if command -v formchk >/dev/null 2>&1; then exec formchk "$1" "$2"; fi; [ -x "$CHEMBOT_FORMCHK_EXE" ] || exit 127; exec "$CHEMBOT_FORMCHK_EXE" "$1" "$2"'
        for chk_path in sorted(set(checkpoints)):
            fchk_path=os.path.splitext(chk_path)[0]+'.fchk'
            try:
                conversion=subprocess.run(['bash','-c',formchk_script,'formchk',chk_path,fchk_path],env=gaussian_env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=max(60,min(remaining_time(),1800)))
                if conversion.returncode==0 and os.path.isfile(fchk_path) and os.path.getsize(fchk_path)>0:
                    converted+=1
                else:
                    send_msg('[GaussView] Could not convert '+os.path.basename(chk_path)+' to .fchk: '+(conversion.stdout or '')[-800:])
            except Exception as exc:
                send_msg('[GaussView] Could not convert '+os.path.basename(chk_path)+' to .fchk: '+str(exc))
        if converted:
            send_msg('[GaussView] Converted %d checkpoint file(s) to Windows-friendly .fchk format.' % converted)
        else:
            send_msg('[GaussView] No usable checkpoint was found. The .log and executed input will still be returned; include a %chk= line to save orbitals.')
        if rc!=0 and not timeout_triggered:
            send_msg('[Kaggle] Gaussian exited with code %d. Available files will still be returned.' % rc)
    else:
        out_text=Path(output_file).read_text(encoding='utf-8',errors='replace') if os.path.exists(output_file) else ''
        analysis=parse_output_text(out_text,output_file)
        analysis['process_returncode']=rc
        if rc!=0 and not timeout_triggered: send_msg(f'[Kaggle] Program exited with code {rc}. Results will still be analyzed and returned.')

        work='analysis_report'; os.makedirs(work,exist_ok=True)
        bundle=generate_report_bundle(analysis,work,basename)
        plots=bundle['plots']; pdf_path=bundle['pdf']

        send_msg('[Analysis]\n'+section_summary(analysis))
        for name,path in plots.items():
            try: tg('sendPhoto',{'chat_id':CHAT_ID,'caption':name.replace('_',' ').title()},'photo',path)
            except Exception as e: send_msg('Could not send plot '+name+': '+str(e))
        try: tg('sendDocument',{'chat_id':CHAT_ID,'caption':'Complete scientific analysis PDF'},'document',pdf_path)
        except Exception as e: send_msg('Could not send PDF: '+str(e))

    results_dir='outputs'; os.makedirs(results_dir,exist_ok=True)
    allowed_ext=('.log','.out','.chk','.fchk','.gjf','.com','.gau','.gbw','.xyz','.molden.input','.property.txt','.property.json','.prop','.hess','.interp','.allxyz','.opt','.dat','.cube','.cub','.engrad','.trj') if is_gaussian else ('.out','.gbw','.xyz','.molden.input','.property.txt','.property.json','.prop','.hess','.interp','.allxyz','.opt','.dat','.cube','.engrad','.trj')
    excluded_roots=[os.path.realpath(results_dir)]
    if os.path.isdir('analysis_report'):
        excluded_roots.append(os.path.realpath('analysis_report'))
    if is_gaussian:
        excluded_roots.append(os.path.realpath(GAUSSIAN_RUNTIME_ROOT))
    for parent,dirs,files in os.walk(os.getcwd()):
        parent_real=os.path.realpath(parent)
        if any(parent_real==root or parent_real.startswith(root+os.sep) for root in excluded_roots):
            dirs[:]=[]
            continue
        dirs[:]=[d for d in dirs if not any(os.path.realpath(os.path.join(parent,d))==root or os.path.realpath(os.path.join(parent,d)).startswith(root+os.sep) for root in excluded_roots)]
        for fn in files:
            if fn==INPUT_FILE or fn.lower().endswith(allowed_ext):
                src=os.path.join(parent,fn)
                if os.path.isfile(src):
                    try:
                        result_name=_windows_safe_basename(fn) if is_gaussian else os.path.basename(fn)
                        result_path=os.path.join(results_dir,result_name)
                        if os.path.exists(result_path) and os.path.realpath(src)!=os.path.realpath(result_path):
                            stem,ext=os.path.splitext(result_name)
                            parent_tag=_windows_safe_basename(os.path.basename(parent))
                            result_path=os.path.join(results_dir,stem+'_'+parent_tag+ext)
                        shutil.copy2(src,result_path)
                    except Exception: pass

    # Include the exact same canonical report bundle that was sent to Telegram.
    if bundle:
        report_out=os.path.join(results_dir,'analysis_report')
        os.makedirs(report_out,exist_ok=True)
        for rp in [bundle.get('pdf'), bundle.get('analysis_json'), bundle.get('manifest')] + list(bundle.get('plots',{}).values()):
            if rp and os.path.isfile(rp):
                try: shutil.copy2(rp, os.path.join(report_out, os.path.basename(rp)))
                except Exception: pass
    result_prefix='Gaussian16_Results_' if is_gaussian else ('Psi4_Results_' if is_psi4 else 'ORCA_Results_')
    archive=shutil.make_archive(result_prefix+basename,'zip',results_dir)
    transfer=send_results_with_fallback(archive,results_dir)
    status=('SUCCESS' if rc==0 else ('TIMEOUT' if timeout_triggered else 'FAILED')) if is_gaussian else ('SUCCESS' if (rc==0 and analysis.get('normal_termination')) else ('TIMEOUT' if timeout_triggered else 'FINISHED WITH WARNINGS/ERROR'))
    if transfer.get('failed'):
        status += ' | PARTIAL_TRANSFER'
    send_msg('[Kaggle] Final status: '+status)
except Exception as e:
    send_msg('Fatal Kaggle error:\n'+str(e)+'\n'+traceback.format_exc()[-2500:])
finally:
    if GAUSSIAN_SCRATCH_DIR:
        shutil.rmtree(GAUSSIAN_SCRATCH_DIR,ignore_errors=True)
'''

def _redact_kaggle_error(text):
    rendered = str(text or '')
    secrets = [KAGGLE_API_TOKEN, KAGGLE_KEY,
               KAGGLE_AUTH_INFO.get('modern_token'), KAGGLE_AUTH_INFO.get('legacy_key')]
    for secret in secrets:
        if secret and len(secret) >= 8:
            rendered = rendered.replace(secret, '[REDACTED]')
    return rendered


def _auth_mode_summary():
    if KAGGLE_AUTH_INFO.get('modern_token') and KAGGLE_AUTH_INFO.get('legacy_key'):
        return 'original dual auth (API token + legacy kaggle.json)'
    if KAGGLE_AUTH_INFO.get('modern_token'):
        return 'original-style API token auth'
    if KAGGLE_AUTH_INFO.get('legacy_key'):
        return 'original-style username/key auth'
    return 'not configured'


def _new_authenticated_kaggle_api(api_factory=None):
    """Use the same KaggleApi.authenticate() pattern as the original bot."""
    if not KAGGLE_USERNAME:
        raise RuntimeError('KAGGLE_USERNAME is empty in Render.')
    if not (KAGGLE_AUTH_INFO.get('modern_token') or KAGGLE_AUTH_INFO.get('legacy_key')):
        raise RuntimeError(
            'No Kaggle credential was found. Set KAGGLE_API_TOKEN or KAGGLE_KEY in Render.'
        )
    factory = api_factory or KaggleApi
    client = factory()
    client.authenticate()
    return client


def submit_kaggle_job(input_name, encoded_files_json, chat_id, is_psi4, extras, drive_link, api_factory=None, is_gaussian=False):
    """Package and push a Kaggle kernel using the original bot's KaggleApi flow."""
    job_id = 'chem-job-' + uuid.uuid4().hex[:16]
    job_dir = None
    try:
        job_dir = tempfile.mkdtemp(prefix=job_id + '_')
        engine = 'gaussian' if is_gaussian else ('psi4' if is_psi4 else 'orca')
        dataset_sources = ([GAUSSIAN_DATASET_SLUG] if is_gaussian else ([] if is_psi4 else [ORCA_DATASET_SLUG]))
        code_file = 'notebook.ipynb' if is_gaussian else 'script.py'
        metadata = {
            'id': f'{KAGGLE_USERNAME}/{job_id}',
            'title': job_id,
            'code_file': code_file,
            'language': 'python',
            'kernel_type': 'notebook' if is_gaussian else 'script',
            'is_private': True,
            'enable_gpu': False,
            'enable_internet': True,
            # Attach only the selected engine's private runtime Dataset.
            'dataset_sources': dataset_sources,
        }
        Path(job_dir, 'kernel-metadata.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding='utf-8', newline='\n'
        )

        header = (
            'BOT_TOKEN=' + repr(BOT_TOKEN) + '\n'
            'CHAT_ID=' + repr(chat_id) + '\n'
            'INPUT_FILE=' + repr(os.path.basename(input_name)) + '\n'
            'ENCODED_FILES_JSON=' + repr(encoded_files_json) + '\n'
            'DRIVE_LINK=' + repr(drive_link) + '\n'
            'PSI4_EXTRAS=' + repr(extras) + '\n'
            'JOB_ENGINE=' + repr(engine) + '\n'
            'KAGGLE_NOTEBOOK_URL=' + repr(f'https://www.kaggle.com/code/{KAGGLE_USERNAME}/{job_id}') + '\n'
        )
        analyzer_code = '' if is_gaussian else ANALYZER_MODULE_CODE
        script = header + '\n' + analyzer_code + '\n' + KAGGLE_RUNNER_CODE
        if is_gaussian:
            notebook = {
                'cells': [{
                    'cell_type': 'code',
                    'execution_count': None,
                    'metadata': {},
                    'outputs': [],
                    'source': script,
                }],
                'metadata': {
                    'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
                    'language_info': {'name': 'python'},
                },
                'nbformat': 4,
                'nbformat_minor': 5,
            }
            Path(job_dir, code_file).write_text(
                json.dumps(notebook, ensure_ascii=False, indent=1),
                encoding='utf-8', newline='\n'
            )
        else:
            Path(job_dir, code_file).write_text(script, encoding='utf-8', newline='\n')

        # Original bot pattern:
        #   api = KaggleApi()
        #   api.authenticate()
        #   api.kernels_push(job_dir)
        job_api = _new_authenticated_kaggle_api(api_factory=api_factory)
        response = job_api.kernels_push(job_dir)

        # Kaggle may return validation information without raising immediately.
        problems=[]
        for attr, label in (
            ('error', 'Kaggle error'),
            ('invalid_dataset_sources', 'Invalid dataset source(s)'),
            ('invalid_kernel_sources', 'Invalid kernel source(s)'),
            ('invalid_competition_sources', 'Invalid competition source(s)'),
        ):
            value = getattr(response, attr, None)
            if value:
                if isinstance(value, (list, tuple)):
                    value = ', '.join(map(str, value))
                problems.append(f'{label}: {value}')
        if problems:
            raise RuntimeError(' ; '.join(problems))

        # Prefer the actual response URL/ref when the API supplies it.
        url = getattr(response, 'url', None)
        ref = getattr(response, 'ref', None)
        if ref and '/' in str(ref):
            owner, slug = str(ref).split('/', 1)
            job_id = slug
            if not url:
                url = f'https://www.kaggle.com/code/{owner}/{slug}'
        if not url:
            url = f'https://www.kaggle.com/code/{KAGGLE_USERNAME}/{job_id}'
        return job_id, str(url)

    except Exception as exc:
        clean = _redact_kaggle_error(exc)
        raise RuntimeError(
            f'Kaggle submission failed for {os.path.basename(input_name)}: {clean}'
        ) from exc
    finally:
        _safe_rmtree(job_dir)


# ------------------------- Bot setup -------------------------
# Prune staging debris before Telegram worker threads start.
_stale_removed = cleanup_stale_local_job_dirs()
if _stale_removed:
    print(f"Removed {len(_stale_removed)} stale ChemBot staging director(ies).")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=BOT_WORKER_THREADS)
try:
    _startup_kaggle_api = _new_authenticated_kaggle_api()
    KAGGLE_STARTUP_AUTH_OK = True
    KAGGLE_STARTUP_AUTH_ERROR = ''
except Exception as _auth_exc:
    KAGGLE_STARTUP_AUTH_OK = False
    KAGGLE_STARTUP_AUTH_ERROR = _redact_kaggle_error(_auth_exc)
print(f"CHEMBOT v6.2.4 is initialized with {BOT_WORKER_THREADS} Telegram workers; Kaggle auth={'OK' if KAGGLE_STARTUP_AUTH_OK else 'FAILED'}")


def authorized(message):
    chat = getattr(message, 'chat', None)
    sender = getattr(message, 'from_user', None)
    chat_id = getattr(chat, 'id', None)
    user_id = getattr(sender, 'id', None)
    return chat_id in ALLOWED_IDS or user_id in ALLOWED_IDS


def split_send(chat_id, text, title=None):
    if title:
        text = f"{title}\n\n{text}"
    text = str(text)
    for i in range(0, len(text), 3900):
        bot.send_message(chat_id, text[i:i+3900])


def out_menu(session_id, a):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        ("📋 Summary", "summary"), ("⚡ Energies", "energy"),
        ("🌡 Thermochemistry", "thermo"), ("〰️ Vibrations / IR", "vib"),
        ("🌈 TD-DFT / UV-Vis", "uv"), ("🧬 Frontier energies", "orb"), ("⚛️ Structure / Charges", "structure"),
        ("📈 Optimization", "optplot"), ("🩺 Diagnostics", "diag"),
        ("📄 Full PDF", "pdf"), ("🖼 All plots", "plots"),
        ("📊 Compare spectra", "compare"),
    ]
    for label, action in buttons:
        # hide unavailable scientific sections
        if action == "thermo" and not a.get("thermochemistry"): continue
        if action == "vib" and not a.get("frequencies_cm1"): continue
        if action == "uv" and not a.get("tddft_states"): continue
        if action == "orb" and not (a.get("orbitals",{}).get("orbitals") or a.get("orbitals",{}).get("homo_ev") is not None): continue
        if action == "optplot" and len(a.get("optimization_energies",[])) < 2: continue
        kb.add(types.InlineKeyboardButton(label, callback_data=f"a:{session_id}:{action}"))
    return kb


def thermo_menu(session_id):
    kb=types.InlineKeyboardMarkup(row_width=2)
    for label,action in [("All thermochemistry","all"),("ZPE","zpe"),("Enthalpy H","h"),("Gibbs G","g"),("Entropy S","s")]:
        kb.add(types.InlineKeyboardButton(label,callback_data=f"t:{session_id}:{action}"))
    return kb


def spectrum_menu(session_id):
    kb=types.InlineKeyboardMarkup(row_width=2)
    for label,action in [("Frequency list","freq"),("IR plot","irplot")]:
        kb.add(types.InlineKeyboardButton(label,callback_data=f"s:{session_id}:{action}"))
    return kb


def uv_menu(session_id):
    kb=types.InlineKeyboardMarkup(row_width=2)
    for label,action in [("Excited states table","states"),("UV-Vis plot","uvplot")]:
        kb.add(types.InlineKeyboardButton(label,callback_data=f"u:{session_id}:{action}"))
    return kb



def comparison_menu(session_id):
    kb=types.InlineKeyboardMarkup(row_width=2)
    for label,action in [("🌈 TD-DFT norm σ10","uv10n"),("🌈 TD-DFT raw σ10","uv10r"),("🌈 TD-DFT norm σ20","uv20n"),("〰️ FT-IR norm σ12","ir12n"),("〰️ FT-IR raw σ12","ir12r"),("〰️ FT-IR norm σ20","ir20n"),("🧹 Clear older analyses","clear")]:
        kb.add(types.InlineKeyboardButton(label,callback_data=f"c:{session_id}:{action}"))
    return kb

def _comparison_analyses(sess, kind):
    with session_lock:
        rows=[x for x in analysis_sessions.values() if x.get('chat_id')==sess.get('chat_id') and x.get('user_id')==sess.get('user_id')]
    rows=sorted(rows,key=lambda x:x.get('created',0))[-10:]
    rows=[x for x in rows if x['analysis'].get('tddft_states' if kind=='uv' else 'ir_spectrum')]
    return [x['analysis'] for x in rows]

def _send_comparison(chat_id,sess,action):
    if action=='clear':
        with session_lock:
            for sid0,x in list(analysis_sessions.items()):
                if x is sess: continue
                if x.get('chat_id')==sess.get('chat_id') and x.get('user_id')==sess.get('user_id'):
                    shutil.rmtree(x.get('dir',''),ignore_errors=True); analysis_sessions.pop(sid0,None)
        bot.send_message(chat_id,'Older comparison analyses cleared.')
        return
    kind='uv' if action.startswith('uv') else 'ir'; sigma=20.0 if '20' in action else (10.0 if kind=='uv' else 12.0); normalize=action.endswith('n')
    arr=_comparison_analyses(sess,kind)
    if len(arr)<2:
        bot.send_message(chat_id,'Upload at least two compatible .out files for this spectrum type.')
        return
    out=os.path.join(sess['dir'],f'comparison_{kind}_{int(sigma)}_{"norm" if normalize else "raw"}.png')
    with render_lock:
        p=make_overlay_plot(arr,kind,out,normalize=normalize,sigma=sigma)
    if not p:
        bot.send_message(chat_id,'Could not create an overlay from the parsed spectra.')
        return
    with open(p,'rb') as f: bot.send_photo(chat_id,f,caption=f'{"TD-DFT / UV-Vis" if kind=="uv" else "FT-IR"} overlay · {len(arr)} files · sigma={sigma:g} · {"normalized" if normalize else "raw"}')

def create_analysis_session(chat_id, user_id, filename, text):
    a=parse_output_text(text,filename)
    if a.get('engine') not in ('ORCA','Psi4'):
        raise ValueError('Could not identify this .out file as ORCA or Psi4 output. Send the original text output, not a converted/trimmed report.')
    sid=uuid.uuid4().hex[:10]
    d=tempfile.mkdtemp(prefix='chembot_analysis_')
    with render_lock:
        bundle=generate_report_bundle(a,d,Path(filename).stem)
        plots=bundle['plots']; pdf=bundle['pdf']
    with session_lock:
        analysis_sessions[sid]={
            'chat_id':chat_id,'user_id':user_id,'analysis':a,'dir':d,
            'plots':plots,'pdf':pdf,'bundle':bundle,'created':time.time()
        }
        # prune old sessions (>6h)
        for old in list(analysis_sessions):
            if time.time()-analysis_sessions[old]['created']>21600:
                try: shutil.rmtree(analysis_sessions[old]['dir'],ignore_errors=True)
                except Exception: pass
                analysis_sessions.pop(old,None)
    return sid,a


def _recent_analyses(chat_id, user_id, max_age_seconds=600, max_items=10):
    now=time.time()
    with session_lock:
        rows=[x for x in analysis_sessions.values() if x.get('chat_id')==chat_id and x.get('user_id')==user_id and now-x.get('created',0)<=max_age_seconds]
    return sorted(rows,key=lambda x:x.get('created',0))[-max_items:]


def _send_group_overlays(chat_id,sessions,group_id):
    analyses=[s['analysis'] for s in sessions]; workdir=sessions[-1]['dir']; produced=[]
    for kind,sigma,label in [('uv',10.0,'TD-DFT / UV-Vis'),('ir',16.0,'FT-IR')]:
        compatible=[a for a in analyses if a.get('tddft_states' if kind=='uv' else 'ir_spectrum')]
        if len(compatible)<2: continue
        out=os.path.join(workdir,f'group_{group_id}_{kind}_overlay.png')
        with render_lock: path=make_overlay_plot(compatible,kind,out,normalize=True,sigma=sigma)
        if path:
            names=', '.join(Path(a.get('filename','spectrum')).stem for a in compatible)
            with open(path,'rb') as fh: bot.send_photo(chat_id,fh,caption=(f'{label} comparison · {len(compatible)} files\n{names}')[:1024])
            produced.append(label)
    return produced


def _finalize_analysis_group(key):
    with analysis_group_lock: batch=analysis_group_batches.pop(key,None)
    if not batch: return
    with session_lock: sessions=[analysis_sessions[sid] for sid in batch.get('sids',[]) if sid in analysis_sessions]
    if not sessions: return
    rows=['📚 Combined analysis group',f'Files: {len(sessions)}','']
    for i,s in enumerate(sessions,1):
        a=s['analysis']; d=a.get('conceptual_dft',{}) or {}; gap=(a.get('orbitals') or {}).get('gap_ev')
        rows.append(f"{i}. {a.get('filename')} — {a.get('engine')} — {'OK' if a.get('normal_termination') else 'not confirmed'}")
        if gap is not None:
            tail=f'gap={gap:.4f} eV'
            if d.get('electronegativity_ev') is not None: tail+=f"; chi={d['electronegativity_ev']:.4f} eV"
            rows.append('   '+tail)
    bot.send_message(batch['chat_id'],'\n'.join(rows)[:3900])
    produced=_send_group_overlays(batch['chat_id'],sessions,batch['group_id'])
    if not produced: bot.send_message(batch['chat_id'],'No spectrum type was present in at least two files, so no overlay was generated.')
    bot.send_message(batch['chat_id'],'Reaction thermodynamics: /reaction A + B -> C + D\nUse .out filename stems as species names.')


def _queue_analysis_group(message,sid):
    gid=getattr(message,'media_group_id',None)
    if not gid: return False
    key=(message.chat.id,message.from_user.id,str(gid))
    with analysis_group_lock:
        batch=analysis_group_batches.setdefault(key,{'chat_id':message.chat.id,'user_id':message.from_user.id,'group_id':str(gid),'sids':[],'timer':None})
        if sid not in batch['sids']: batch['sids'].append(sid)
        if batch.get('timer'):
            try: batch['timer'].cancel()
            except Exception: pass
        timer=threading.Timer(ANALYSIS_GROUP_DEBOUNCE_SECONDS,_finalize_analysis_group,args=(key,)); timer.daemon=True; batch['timer']=timer; timer.start()
    return True


def _parse_reaction_equation(equation):
    parts=re.split(r'\s*(?:<=>|<->|-->|->|=>|=)\s*',equation.strip())
    if len(parts)!=2 or not all(parts): raise ValueError('Use reaction syntax: A + 2 B -> C + D')
    def side(s):
        out=[]
        for raw in s.split('+'):
            raw=raw.strip(); m=re.match(r'^(?:(\d+(?:\.\d+)?)\s*\*?\s*)?(.+?)$',raw)
            if not m: raise ValueError('Could not parse reaction term: '+raw)
            coeff=float(m.group(1) or 1.0)
            if not math.isfinite(coeff) or coeff<=0:
                raise ValueError('Stoichiometric coefficients must be positive and finite.')
            out.append((coeff,m.group(2).strip()))
        return out
    return side(parts[0]),side(parts[1])


def _reaction_thermo_from_sessions(chat_id,user_id,equation):
    reactants,products=_parse_reaction_equation(equation)
    rows=_recent_analyses(chat_id,user_id,max_age_seconds=21600,max_items=50)
    byname={Path(x['analysis'].get('filename','')).stem.lower():x['analysis'] for x in rows}
    def resolve(name):
        key=Path(name).stem.lower()
        if key not in byname: raise ValueError(f'No parsed output found for {name!r}; upload its .out first.')
        return byname[key]
    species=[resolve(name) for _,name in reactants+products]
    if any(not a.get('normal_termination') for a in species):
        raise ValueError('Every participating output must confirm normal termination before reaction thermodynamics is calculated.')
    warnings=[]
    levels={(a.get('method') or 'N/A',a.get('basis') or 'N/A') for a in species}
    comparable=len(levels)==1
    if not comparable:
        warnings.append('Mixed levels of theory: '+', '.join(f'{m}/{b}' for m,b in sorted(levels)))
    solvation={(a.get('solvation_model'),a.get('solvent')) for a in species}
    if len(solvation)>1:
        warnings.append('Solvation conditions differ or were not identified for every species.')
        comparable=False
    if any(not a.get('method') or not a.get('basis') for a in species):
        warnings.append('Method/basis could not be verified for every species.')
        comparable=False

    def atom_counts(a):
        counts={}
        for row in a.get('final_geometry',[]) or []:
            element=row.get('element') if isinstance(row,dict) else (row[0] if isinstance(row,(list,tuple)) and row else None)
            if not element: continue
            raw_symbol=str(element).strip()
            if raw_symbol.endswith(':'): continue
            symbol=raw_symbol.capitalize()
            if symbol in ('Da','X','Gh'): continue
            if not re.fullmatch(r'[A-Z][a-z]?',symbol): continue
            counts[symbol]=counts.get(symbol,0.0)+1.0
        return counts
    balance={}; geometry_complete=True
    for sign,terms in ((-1.0,reactants),(1.0,products)):
        for coeff,name in terms:
            counts=atom_counts(resolve(name))
            if not counts:
                geometry_complete=False
                continue
            for element,n in counts.items():
                balance[element]=balance.get(element,0.0)+sign*coeff*n
    if geometry_complete:
        imbalance={k:v for k,v in balance.items() if abs(v)>1e-6}
        if imbalance:
            details=', '.join(f'{k}{v:+g}' for k,v in sorted(imbalance.items()))
            raise ValueError('Reaction is not atom balanced (products-reactants): '+details)
    else:
        warnings.append('Atom balance could not be checked because at least one final geometry is missing.')

    charges=[]
    for sign,terms in ((-1.0,reactants),(1.0,products)):
        for coeff,name in terms:
            q=_float((resolve(name).get('system') or {}).get('charge'))
            if q is not None and math.isfinite(q):
                charges.append(sign*coeff*q)
    if len(charges)!=len(species):
        warnings.append('Charge balance could not be checked because at least one charge is missing.')
    elif abs(sum(charges))>1e-6:
        raise ValueError(f'Reaction is not charge balanced (products-reactants): {sum(charges):+g}.')

    def delta(getter):
        total=0.0
        for coeff,name in products:
            v=_float(getter(resolve(name)))
            if v is None or not math.isfinite(v): return None
            total+=coeff*v
        for coeff,name in reactants:
            v=_float(getter(resolve(name)))
            if v is None or not math.isfinite(v): return None
            total-=coeff*v
        return total
    e=delta(lambda a:(a.get('energies') or {}).get('final_energy_hartree'))
    e0=delta(lambda a: ((a.get('energies') or {}).get('final_energy_hartree')+(a.get('thermochemistry') or {}).get('zpe_hartree')) if (a.get('energies') or {}).get('final_energy_hartree') is not None and (a.get('thermochemistry') or {}).get('zpe_hartree') is not None else None)
    temps=[_float((a.get('thermochemistry') or {}).get('temperature_K')) for a in species]
    temp=temps[0] if (all(t is not None and math.isfinite(t) and t>0 for t in temps)
                       and max(temps)-min(temps)<1e-4) else None
    if temp is None:
        warnings.append('Temperature is missing or inconsistent; Delta H, Delta G, Delta S and K_eq were not evaluated.')
    h=delta(lambda a:(a.get('thermochemistry') or {}).get('enthalpy_hartree')) if temp is not None else None
    g=delta(lambda a:(a.get('thermochemistry') or {}).get('gibbs_hartree')) if temp is not None else None
    if not comparable:
        e=e0=h=g=None
        warnings.append('Reaction differences were not evaluated because theory or solvation conditions are inconsistent or incomplete.')
    conv=HARTREE_TO_KJMOL
    out={'equation':equation,'delta_e_kj_mol':e*conv if e is not None else None,'delta_e0_kj_mol':e0*conv if e0 is not None else None,'delta_h_kj_mol':h*conv if h is not None else None,'delta_g_kj_mol':g*conv if g is not None else None,'temperature_K':temp}
    out['delta_s_j_mol_k']=((h-g)*conv*1000.0/temp) if h is not None and g is not None and temp and temp>0 else None
    if g is not None and temp and comparable:
        try: out['keq']=math.exp(-(g*conv*1000.0)/(8.314462618*temp))
        except OverflowError: out['keq']=float('inf') if g<0 else 0.0
    else:
        out['keq']=None
    out['warnings']=warnings
    return out


@bot.message_handler(commands=['reaction'])
def reaction_command(message):
    if not authorized(message): return
    equation=(message.text or '').partition(' ')[2].strip()
    if not equation:
        bot.reply_to(message,'Usage: /reaction A + B -> C + D\nNames must match uploaded .out filename stems.'); return
    try:
        r=_reaction_thermo_from_sessions(message.chat.id,message.from_user.id,equation)
        lines=[f"Reaction: {r['equation']}"]
        for k,label in [('delta_e_kj_mol','Delta Eel'),('delta_e0_kj_mol','Delta(Eel+ZPE)'),('delta_h_kj_mol','Delta H'),('delta_g_kj_mol','Delta G')]:
            lines.append(f"{label}: {r[k]:.3f} kJ mol^-1" if r.get(k) is not None else f'{label}: unavailable')
        lines.append(f"Delta S: {r['delta_s_j_mol_k']:.3f} J mol^-1 K^-1" if r.get('delta_s_j_mol_k') is not None else 'Delta S: unavailable')
        if r.get('temperature_K') is not None: lines.append(f"T: {r['temperature_K']:.2f} K")
        if r.get('keq') is not None: lines.append(f"K_eq: {r['keq']:.6e}")
        if r.get('warnings'):
            lines.append('Warnings:')
            lines.extend('• '+w for w in r['warnings'])
        lines.append('Use mutually consistent levels of theory and thermochemical conditions across all species.')
        bot.reply_to(message,'\n'.join(lines))
    except Exception as exc: bot.reply_to(message,'Reaction thermodynamics error: '+str(exc))


@bot.message_handler(commands=['start'])
def start(message):
    if not authorized(message): return
    uid=message.from_user.id
    with state_lock:
        user_aux_storage.pop(uid,None)
        user_drive_links.pop(uid,None)
    bot.reply_to(message,
        "🧪 Computational Chemistry Bot v6.2.4\n"
        f"• Kaggle authentication mode: {_auth_mode_summary()}\n\n"
        "• Send ORCA .inp, Psi4 .dat, or a GaussianView .gjf/.com/.gau file to run on private Kaggle jobs.\n"
        "• The private Kaggle run link arrives when the calculation process starts.\n"
        "• Send ORCA/Psi4 .out for scientific analysis, plots and PDF. Gaussian returns .log/.chk/.fchk for GaussianView 6.\n"
        "• Upload multiple .out files to overlay TD-DFT/UV-Vis or FT-IR spectra.\n"
        "• Send .xyz/.allxyz/.gbw/.chk/.fchk before jobs when needed; the same snapshot is available to the whole batch.\n"
        "• Psi4 D3/D4/gCP/geomeTRIC dependencies are detected and installed automatically.\n"
        "• Use /clearaux after a batch to clear stored auxiliary files/restart URL.\n"
        "• Kaggle sends calculation results directly, so this launcher can be closed after all submissions are confirmed."
    )


@bot.message_handler(commands=['version'])
def version_command(message):
    if not authorized(message): return
    bot.reply_to(
        message,
        'ChemBot build: v6.2.4-STRICT-20260927\n'
        f'Kaggle username configured: {"yes" if bool(KAGGLE_USERNAME) else "no"}\n'
        f'Authentication mode: {_auth_mode_summary()}\n'
        f'Legacy kaggle.json prepared: {"yes" if bool(KAGGLE_AUTH_INFO.get("legacy_key")) else "no"}\n'
        f'API access_token prepared: {"yes" if bool(KAGGLE_AUTH_INFO.get("modern_token")) else "no"}\n'
        f'Startup KaggleApi.authenticate(): {"OK" if KAGGLE_STARTUP_AUTH_OK else "FAILED"}\n'
        f'ORCA dataset: {ORCA_DATASET_SLUG}\n'
        f'Gaussian dataset: {GAUSSIAN_DATASET_SLUG}'
    )


@bot.message_handler(commands=['help'])
def help_command(message):
    if not authorized(message): return
    bot.reply_to(message,
        "Supported inputs: ORCA .inp; Psi4 .dat; Gaussian .gjf/.com/.gau or a Gaussian route-card .inp.\n"
        "Auxiliary/restart inputs: .xyz, .allxyz, .gbw, .chk, .fchk.\n"
        "Analysis input: ORCA/Psi4 .out. Gaussian .log is returned without an analyzer.\n"
        "Use /clearaux to discard stored auxiliary/restart context after a batch.\n\n"
        "For .out files the bot extracts every recognized section and exposes interactive menus for energies, thermochemistry, frequencies/IR, TD-DFT/UV-Vis, orbital energies, optimization profile and diagnostics. Upload two or more compatible outputs to overlay TD-DFT/UV-Vis or FT-IR spectra. A PDF with generated figures is also produced."
    )


@bot.message_handler(commands=['clearaux'])
def clear_aux(message):
    if not authorized(message): return
    uid = message.from_user.id
    with state_lock:
        n = len(user_aux_storage.get(uid, {}))
        had_link = bool(user_drive_links.get(uid))
        user_aux_storage.pop(uid, None)
        user_drive_links.pop(uid, None)
    bot.reply_to(message, f"🧹 Cleared {n} auxiliary file(s)" + (" and the restart URL." if had_link else "."))


@bot.message_handler(func=lambda m: bool(m.text and m.text.startswith(('http://','https://'))))
def handle_link(message):
    if not authorized(message): return
    if not (message.text or '').strip().lower().startswith('https://'):
        bot.reply_to(message,'Restart archive URLs must use HTTPS.'); return
    with state_lock:
        user_drive_links[message.from_user.id]=message.text.strip()
    bot.reply_to(message,"🔗 Restart archive URL stored. It will be snapshotted into each subsequent job until /clearaux or /start.")


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    try:
        parts=call.data.split(':')
        if len(parts)!=3: return
        kind,sid,action=parts
        with session_lock: sess=analysis_sessions.get(sid)
        if not sess:
            bot.answer_callback_query(call.id,"Analysis session expired.",show_alert=True); return
        if call.message.chat.id != sess['chat_id']:
            bot.answer_callback_query(call.id,"This analysis belongs to another chat.",show_alert=True); return
        a=sess['analysis']; bot.answer_callback_query(call.id)
        if kind=='a':
            if action=='summary': split_send(call.message.chat.id,section_summary(a),'📋 Summary')
            elif action=='energy': split_send(call.message.chat.id,section_energies(a),'⚡ Energies')
            elif action=='thermo': bot.send_message(call.message.chat.id,"Choose thermochemical data:",reply_markup=thermo_menu(sid))
            elif action=='vib': bot.send_message(call.message.chat.id,"Choose vibrational output:",reply_markup=spectrum_menu(sid))
            elif action=='uv': bot.send_message(call.message.chat.id,"Choose electronic-spectrum output:",reply_markup=uv_menu(sid))
            elif action=='orb': split_send(call.message.chat.id,section_orbitals(a),'🧬 Frontier orbital energies')
            elif action=='structure': split_send(call.message.chat.id,section_structure(a),'⚛️ Structure / charges / dipole')
            elif action=='diag': split_send(call.message.chat.id,section_diagnostics(a),'🩺 Diagnostics')
            elif action=='optplot':
                p=sess['plots'].get('optimization');
                if p:
                    with open(p,'rb') as f: bot.send_photo(call.message.chat.id,f,caption='Optimization energy profile')
            elif action=='plots':
                for name,p in sess['plots'].items():
                    with open(p,'rb') as f: bot.send_photo(call.message.chat.id,f,caption=name.replace('_',' ').title())
            elif action=='pdf':
                with open(sess['pdf'],'rb') as f: bot.send_document(call.message.chat.id,f,caption='Complete computational chemistry analysis report')
            elif action=='compare': bot.send_message(call.message.chat.id,'Choose overlay type. The latest compatible .out analyses (up to 10) are used:',reply_markup=comparison_menu(sid))
        elif kind=='t': split_send(call.message.chat.id,section_thermo(a,action),'🌡 Thermochemistry')
        elif kind=='s':
            if action=='freq': split_send(call.message.chat.id,section_vibrations(a),'〰️ Vibrational frequencies')
            elif action=='irplot':
                p=sess['plots'].get('ir')
                if p:
                    with open(p,'rb') as f: bot.send_photo(call.message.chat.id,f,caption='Calculated IR spectrum')
                else: bot.send_message(call.message.chat.id,'IR intensities were not recognized in this output.')
        elif kind=='c':
            _send_comparison(call.message.chat.id,sess,action)
        elif kind=='u':
            if action=='states': split_send(call.message.chat.id,section_uv(a),'🌈 TD-DFT / UV-Vis states')
            elif action=='uvplot':
                p=sess['plots'].get('uvvis')
                if p:
                    with open(p,'rb') as f: bot.send_photo(call.message.chat.id,f,caption='Simulated UV-Vis spectrum')
                else: bot.send_message(call.message.chat.id,'No UV-Vis plot is available.')
    except Exception as e:
        error_id=uuid.uuid4().hex[:10]
        print('Callback failure reference',error_id,':',repr(e))
        try: bot.answer_callback_query(call.id,'Could not complete this action. Please retry.',show_alert=True)
        except Exception: pass


@bot.message_handler(content_types=['document'])
def handle_document(message):
    if not authorized(message): return
    uid=message.from_user.id; chat_id=message.chat.id
    original_name=message.document.file_name or 'file'
    filename=original_name.lower()
    size=message.document.file_size or 0

    # Direct scientific analysis of ORCA/Psi4 output
    if filename.endswith('.out'):
        if size>MAX_TEXT_OUT:
            bot.reply_to(message,'The .out file exceeds the current 20 MB direct-analysis safety limit.'); return
        try:
            info=bot.get_file(message.document.file_id)
            raw=bot.download_file(info.file_path)
            text=raw.decode('utf-8',errors='replace')
            detected_engine=detect_engine(text)
            if detected_engine not in ('ORCA','Psi4'):
                bot.reply_to(message,'❌ This .out file could not be identified as ORCA or Psi4 output. Please send the original plain-text .out file without conversion or truncation.')
                return
            bot.reply_to(message,f'🔬 {detected_engine} output detected. Parsing scientific results and generating figures/PDF...')
            sid,a=create_analysis_session(chat_id,uid,original_name,text)
            if not _queue_analysis_group(message,sid):
                bot.send_message(chat_id,section_summary(a),reply_markup=out_menu(sid,a))
        except Exception as e:
            import traceback
            print('Direct .out analysis failure for', original_name)
            traceback.print_exc()
            error_id=uuid.uuid4().hex[:10]
            bot.reply_to(message,'Analysis failed while processing this output. Please retry or resend it. Reference: '+error_id)
        return

    # Auxiliary job files
    if filename.endswith(('.xyz','.allxyz','.gbw','.chk','.fchk')):
        if size>MAX_TELEGRAM_DOWNLOAD:
            bot.reply_to(message,'This auxiliary file exceeds Telegram Bot API 20 MB download limit. Use a restart ZIP URL instead.'); return
        try:
            info=bot.get_file(message.document.file_id); raw=bot.download_file(info.file_path)
            with state_lock:
                bucket=user_aux_storage.setdefault(uid,{})
                projected=sum(len(base64.b64decode(v)) for v in bucket.values())+len(raw)
                if projected>MAX_AUX_STORAGE:
                    too_large=True
                    current_files=list(bucket)
                else:
                    too_large=False
                    bucket[os.path.basename(original_name)]=base64.b64encode(raw).decode('ascii')
                    current_files=list(bucket)
            if too_large:
                bot.reply_to(message,'Auxiliary in-memory files would exceed 20 MB. Send a restart ZIP URL for larger data.'); return
            bot.reply_to(message,f"✅ Stored {original_name}. Current auxiliary files: {', '.join(current_files)}. They remain available to every submitted job until /clearaux or /start.")
        except Exception as e: bot.reply_to(message,'Upload error: '+str(e))
        return

    # Calculation jobs
    if filename.endswith(('.inp','.dat','.gjf','.com','.gau')):
        if size>MAX_TELEGRAM_DOWNLOAD:
            bot.reply_to(message,'Input exceeds Telegram Bot API download limit.'); return
        try:
            info=bot.get_file(message.document.file_id); raw=bot.download_file(info.file_path); inp=decode_job_input(raw)
            engine=detect_job_engine(original_name,inp)
            if engine is None:
                bot.reply_to(message,'Unsupported calculation input. Send ORCA .inp, Psi4 .dat, or Gaussian .gjf/.com/.gau input.'); return
            is_psi4=engine=='psi4'; is_gaussian=engine=='gaussian'
            extras=detect_psi4_extras(inp) if is_psi4 else []
            prog='Gaussian 16' if is_gaussian else ('Psi4' if is_psi4 else 'ORCA')
            extra_note=("\nDetected external Psi4 packages: "+', '.join(extras)) if extras else ''
            bot.reply_to(message,f"☁️ {prog} input received. Preparing Kaggle job...{extra_note}")

            # Take one immutable context snapshot for this message. Multiple
            # simultaneous .inp/.dat handlers therefore cannot consume each
            # other's auxiliary files or restart URL.
            aux_snapshot, drive = snapshot_user_job_context(uid)

            # Helpful NEB end-point convenience, retaining original behavior.
            if engine=='orca' and 'neb-ts' in inp.lower() and '%neb' not in inp.lower():
                xyzs=[n for n in aux_snapshot if n.lower().endswith(('.xyz','.allxyz'))]
                if len(xyzs)==1:
                    inp += f'\n\n%neb\n  NEB_End_XYZ "{xyzs[0]}"\nend\n'
                    bot.send_message(chat_id,f"NEB-TS endpoint automatically linked: {xyzs[0]}")

            payload={os.path.basename(original_name):base64.b64encode(inp.encode()).decode('ascii')}
            payload.update(aux_snapshot)
            encoded=base64.b64encode(json.dumps(payload).encode()).decode('ascii')
            # submit_kaggle_job uses a UUID slug, a private tempfile directory,
            # a fresh KaggleApi client, and a finally-cleanup for this job only.
            job_id,_url=submit_kaggle_job(
                original_name, encoded, chat_id, is_psi4, extras, drive,
                is_gaussian=is_gaussian
            )
            bot.send_message(chat_id,f"✅ Private Kaggle {'notebook' if is_gaussian else 'job'} submitted for {prog}: {original_name}\nYou may close this launcher after all files in the batch show this confirmation. The notebook link will be sent when the calculation process starts.")
        except Exception as e:
            bot.reply_to(message,'Submission error: '+str(e))
        return

    bot.reply_to(message,'Supported files: ORCA .inp, Psi4 .dat, Gaussian .gjf/.com/.gau (or Gaussian route-card .inp), .out, .xyz, .allxyz, .gbw, .chk, .fchk')


# ------------------------- Runtime transport -------------------------
# Telegram permits only one active getUpdates poller for a bot token. Render
# performs zero-downtime deploys by starting the new instance before stopping
# the old one, so long polling can briefly create two pollers and Telegram then
# returns HTTP 409.  On a Render *web service* we therefore use a webhook.
# Local/manual runs keep the convenient polling mode.


def _transport_mode():
    mode = os.environ.get("CHEMBOT_MODE", "auto").strip().lower()
    if mode in {"webhook", "polling"}:
        return mode
    # Render web services expose a public hostname/URL. A manual public URL
    # can also be supplied for other hosts.
    if (os.environ.get("CHEMBOT_PUBLIC_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()):
        return "webhook"
    return "polling"


def _webhook_secret():
    configured = os.environ.get("CHEMBOT_WEBHOOK_SECRET", "").strip()
    if configured:
        return configured
    # Stable across overlapping Render instances, but never printed or exposed.
    return hashlib.sha256((BOT_TOKEN + "|ChemBot|Webhook|v4.7").encode("utf-8")).hexdigest()[:48]


def _webhook_path():
    configured = os.environ.get("CHEMBOT_WEBHOOK_PATH", "").strip().strip("/")
    if configured:
        return "/" + configured
    token_hash = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()[:24]
    return "/telegram/" + token_hash


def run_render_webhook():
    external_url = os.environ.get("CHEMBOT_PUBLIC_URL", "").strip().rstrip("/")
    if not external_url:
        external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not external_url:
        host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
        if host:
            external_url = "https://" + host
    if not external_url:
        raise RuntimeError(
            "Webhook mode requires CHEMBOT_PUBLIC_URL, RENDER_EXTERNAL_URL, or "
            "RENDER_EXTERNAL_HOSTNAME. On Render, deploy as a Web Service."
        )

    secret = _webhook_secret()
    path = _webhook_path()
    webhook_url = external_url + path
    port = int(os.environ.get("PORT", "10000"))
    max_update_bytes = int(os.environ.get("CHEMBOT_MAX_WEBHOOK_BYTES", str(2 * 1024 * 1024)))

    class TelegramWebhookHandler(BaseHTTPRequestHandler):
        server_version = "ChemBotWebhook/4.7"

        def log_message(self, fmt, *args):
            # Keep Render logs compact and avoid printing the secret path.
            print("[webhook] " + (fmt % args))

        def _reply(self, status, body=b"OK", content_type="text/plain; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                payload = json.dumps({
                    "ok": True,
                    "service": "ChemBot",
                    "version": "5.3",
                    "transport": "webhook",
                }).encode("utf-8")
                return self._reply(200, payload, "application/json; charset=utf-8")
            return self._reply(404, b"Not Found")

        def do_POST(self):
            if self.path != path:
                return self._reply(404, b"Not Found")

            header_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header_secret != secret:
                return self._reply(403, b"Forbidden")

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400, b"Bad Content-Length")

            if content_length <= 0 or content_length > max_update_bytes:
                return self._reply(413, b"Payload Too Large")

            try:
                raw = self.rfile.read(content_length)
                update = telebot.types.Update.de_json(raw.decode("utf-8"))
                # TeleBot is configured threaded=True; handlers are delegated to
                # its worker pool, allowing the HTTP request to acknowledge fast.
                bot.process_new_updates([update])
            except Exception as exc:
                print(f"Webhook update processing error: {exc!r}")
                return self._reply(500, b"Update processing failed")

            return self._reply(200, b"OK")

    server = ThreadingHTTPServer(("0.0.0.0", port), TelegramWebhookHandler)

    # setWebhook replaces any previous webhook atomically. Both the old and new
    # Render instance use the same URL/secret during zero-downtime deployment,
    # so there is no getUpdates race and no need to delete the webhook first.
    ok = bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        drop_pending_updates=False,
    )
    if not ok:
        server.server_close()
        raise RuntimeError("Telegram setWebhook returned false.")

    print(f"CHEMBOT v6.2.4 webhook mode active on port {port}.")
    print(f"Health check: {external_url}/health")

    shutting_down = threading.Event()

    def _shutdown(signum, frame):
        del frame
        if shutting_down.is_set():
            return
        shutting_down.set()
        print(f"Received signal {signum}; shutting down webhook server gracefully...")
        # Do not delete the webhook: during Render zero-downtime deploy the new
        # instance is already serving the exact same webhook URL.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, _shutdown)
            except Exception:
                pass

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        print("Webhook server stopped.")


def run_polling():
    # Polling is intended for one manual/local instance only.  Production
    # webhooks are NOT removed by default; set CHEMBOT_REMOVE_WEBHOOK_ON_POLL=1
    # explicitly only when intentionally moving the same token back to polling.
    remove = os.environ.get("CHEMBOT_REMOVE_WEBHOOK_ON_POLL", "0").strip().lower() not in {"0", "false", "no"}
    if remove:
        try:
            bot.remove_webhook()
            time.sleep(0.5)
        except Exception as exc:
            print(f"Warning: could not remove old webhook before polling: {exc}")

    print("CHEMBOT v6.2.4 polling mode active. Ensure no other instance uses this bot token.")
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except telebot.apihelper.ApiTelegramException as exc:
        if getattr(exc, "error_code", None) == 409 or "other getUpdates request" in str(exc):
            raise RuntimeError(
                "Telegram 409 conflict: another process is already polling this bot token. "
                "Stop the other local/Render bot instance, or deploy ChemBot as a Render Web Service "
                "so the configured webhook transport can be used."
            ) from exc
        raise


if __name__ == '__main__':
    mode = _transport_mode()
    print(f"ChemBot transport selected: {mode}")
    if mode == "webhook":
        run_render_webhook()
    else:
        run_polling()
