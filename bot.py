# BUILD: v5.4-KAGGLE-AUTH-FALLBACK-20260926
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

# Modern API-token auth is preferred. Do NOT mirror it into KAGGLE_KEY;
# keeping both auth schemes active can make a CLI/runtime fall back to legacy.
if KAGGLE_USERNAME:
    os.environ['KAGGLE_USERNAME'] = KAGGLE_USERNAME
if KAGGLE_API_TOKEN:
    os.environ['KAGGLE_API_TOKEN'] = KAGGLE_API_TOKEN
    os.environ.pop('KAGGLE_KEY', None)
elif KAGGLE_KEY:
    os.environ['KAGGLE_KEY'] = KAGGLE_KEY
    os.environ.pop('KAGGLE_API_TOKEN', None)

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
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ============================================================
# Chemistry Telegram/Kaggle Bot v5.4
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

# Re-assert after pip/import activity. No Kaggle module is invoked before here.
if KAGGLE_USERNAME:
    os.environ['KAGGLE_USERNAME'] = KAGGLE_USERNAME
if KAGGLE_API_TOKEN:
    os.environ['KAGGLE_API_TOKEN'] = KAGGLE_API_TOKEN
    os.environ.pop('KAGGLE_KEY', None)
elif KAGGLE_KEY:
    os.environ['KAGGLE_KEY'] = KAGGLE_KEY

import telebot
from telebot import types

if not BOT_TOKEN:
    raise RuntimeError("Set CHEMBOT_BOT_TOKEN before running the bot.")

ALLOWED_IDS = {7495822836, -1003907097817, 839801823, -1003925918657, -1003875125323}
ADMIN_ID = 839801823
ORCA_DATASET_SLUG = os.environ.get(
    "ORCA_DATASET_SLUG", "abdulsalsmsalih/orca-6-1-0"
)
MAX_TELEGRAM_DOWNLOAD = 20 * 1024 * 1024
MAX_AUX_STORAGE = 20 * 1024 * 1024
MAX_TEXT_OUT = 20 * 1024 * 1024

user_aux_storage = {}
user_drive_links = {}
analysis_sessions = {}
# Protect short-lived shared state used by Telegram handler threads.
session_lock = threading.RLock()
state_lock = threading.RLock()
render_lock = threading.RLock()  # Matplotlib/ReportLab rendering is serialized for thread safety.

# Telegram can deliver several documents almost simultaneously.  Use enough
# worker threads to accept a batch, while every Kaggle submission receives its
# own KaggleApi instance and its own temporary directory.
BOT_WORKER_THREADS = max(2, int(os.environ.get("CHEMBOT_WORKER_THREADS", "8")))

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
import os, re, math, json, textwrap
from pathlib import Path

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


def build_pdf(a, plots, pdf_path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
    styles=getSampleStyleSheet(); mono=ParagraphStyle('mono',parent=styles['BodyText'],fontName='Courier',fontSize=7.5,leading=9)
    doc=SimpleDocTemplate(pdf_path,pagesize=A4,rightMargin=1.4*cm,leftMargin=1.4*cm,topMargin=1.3*cm,bottomMargin=1.3*cm)
    story=[Paragraph('Computational Chemistry Results Report',styles['Title']),Spacer(1,8),Paragraph(f"File: {a.get('filename','')}",styles['BodyText'])]
    meta=[['Engine',a.get('engine')],['Version',a.get('version') or 'N/A'],['Status','Normal termination' if a.get('normal_termination') else 'Not confirmed'],['Method',a.get('method') or 'N/A'],['Basis',a.get('basis') or 'N/A']]
    tab=Table(meta,colWidths=[4*cm,12*cm]); tab.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.25,colors.grey),('BACKGROUND',(0,0),(0,-1),colors.whitesmoke),('VALIGN',(0,0),(-1,-1),'TOP') ])); story += [Spacer(1,10),tab,Spacer(1,12)]
    sections=[('Summary',section_summary(a)),('Energies',section_energies(a)),('Thermochemistry',section_thermo(a)),('Vibrational analysis',section_vibrations(a)),('TD-DFT / UV-Vis',section_uv(a)),('Orbital energies',section_orbitals(a)),('Structure, charges and dipole',section_structure(a)),('Diagnostics',section_diagnostics(a))]
    for title,body in sections:
        if body and not body.startswith('No '):
            story += [Paragraph(title,styles['Heading2']),Paragraph(body.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'),mono),Spacer(1,10)]
    for title,path in plots.items():
        if os.path.exists(path):
            story += [PageBreak(),Paragraph(title.replace('_',' ').title(),styles['Heading2']),Spacer(1,5),Image(path,width=17*cm,height=10.5*cm)]
    doc.build(story)
    return pdf_path
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
    rx_irrow = re.compile(rf'^\s*(?P<mode>\d+):\s*(?P<freq>{F})\s+(?P<t2>{F})', re.I)

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
                continue
            m = rx_irrow.search(stripped)
            if m:
                freq = _float(m.group('freq'))
                inten = _float(m.group('t2'))
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
    if normal is False or (errors and normal is not True):
        sp_status='FAILED_CALCULATION'; thermo_rel='FAILED_CALCULATION'
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
        if 'EXCITATION ENERGY' in u and 'OSCILLATOR STRENGTH' in u: active=True; continue
        if active:
            m=re.match(r'^\s*(\d+)\s+',line)
            if not m:
                if states and line.strip() and '----' not in line: active=False
                continue
            vals=_nums(line[m.end():])
            if len(vals)>=4:
                ev=vals[1]; states.append({'state':int(m.group(1)),'ev':ev,'nm':1239.841984/ev if ev and ev>0 else None,'f':vals[3],'f_velocity':vals[4] if len(vals)>4 else None,'excitation_au':vals[0]})
    return states

def _parse_psi4_precise(text, filename):
    F=FLOAT_RE
    version=None
    for rx in [r'Psi4\s+([0-9][\w.\-]+)',r'Psi4\s+Version\s*[:=]?\s*([0-9][\w.\-]+)']:
        m=re.search(rx,text,re.I)
        if m: version=m.group(1); break
    normal=bool(re.search(r'Psi4\s+exiting successfully',text,re.I))
    fatal=[ln.strip() for ln in text.splitlines() if re.search(r'(PSIEXCEPTION|TRACEBACK|FATAL ERROR|SEGMENTATION FAULT|OUT OF MEMORY|CONVERGENCE FAILURE)',ln,re.I)]
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
ANALYZER_MODULE_CODE = ANALYZER_MODULE_CODE + "\n" + ANALYZER_V41_PATCH_CODE

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
import os, sys, json, time, base64, shutil, zipfile, tarfile, glob, subprocess, traceback, urllib.request, urllib.parse, hashlib
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
    try: tg('sendMessage',{'chat_id':CHAT_ID,'text':str(msg)[:3900]})
    except Exception: pass

def safe_install(cmd):
    send_msg('[Kaggle] Installing: '+' '.join(cmd))
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if p.returncode!=0:
        raise RuntimeError('Dependency installation failed: '+' '.join(cmd)+'\n'+p.stdout[-2500:])

def remaining_time():
    return max(60, HARD_LIMIT-SAFETY_MARGIN-(time.time()-START_TIME))

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
    priority={'.out':0,'.property.txt':1,'.property.json':2,'.xyz':3,'.hess':4,'.gbw':5,'.molden.input':6,'.engrad':7,'.opt':8,'.trj':9,'.allxyz':10,'.cube':11,'.dat':12}
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

try:
    safe_install([sys.executable,'-m','pip','install','-q','requests','matplotlib>=3.7','reportlab>=4.0','numpy'])
    send_msg('[Kaggle] Session connected. Preparing calculation...')

    files_dict=json.loads(base64.b64decode(ENCODED_FILES_JSON).decode('utf-8'))
    for fname,b64content in files_dict.items():
        # strip path traversal
        safe_name=os.path.basename(fname)
        with open(safe_name,'wb') as f: f.write(base64.b64decode(b64content))
    INPUT_FILE=os.path.basename(INPUT_FILE)

    if DRIVE_LINK:
        send_msg('[Kaggle] Downloading restart archive...')
        req=urllib.request.Request(DRIVE_LINK,headers={'User-Agent':'ChemBot/4.0'})
        with urllib.request.urlopen(req,timeout=120) as r, open('restart_files.zip','wb') as f:
            shutil.copyfileobj(r,f)
        if os.path.getsize('restart_files.zip')>1024*1024*1024:
            raise RuntimeError('Restart archive exceeds 1 GB safety limit.')
        with zipfile.ZipFile('restart_files.zip') as z:
            for info in z.infolist():
                target=os.path.abspath(os.path.join('.',info.filename))
                if not target.startswith(os.path.abspath('.')+os.sep):
                    raise RuntimeError('Unsafe ZIP path detected.')
            z.extractall('.')
        os.remove('restart_files.zip')

    is_psi4=INPUT_FILE.lower().endswith('.dat')
    basename=os.path.splitext(INPUT_FILE)[0]
    output_file=basename+'.out'
    if is_psi4:
        send_msg('[Kaggle] Preparing Psi4 environment...')
        psi4_exe=shutil.which('psi4')
        if not psi4_exe:
            mgr=shutil.which('mamba') or shutil.which('conda')
            if not mgr:
                raise RuntimeError('Neither mamba nor conda is available in this Kaggle image, so Psi4 cannot be installed.')
            safe_install([mgr,'install','-y','-q','-c','conda-forge','psi4'])
            psi4_exe=shutil.which('psi4')
        if PSI4_EXTRAS:
            mgr=shutil.which('mamba') or shutil.which('conda')
            if not mgr:
                raise RuntimeError('Psi4 extras were requested but no conda-compatible package manager is available.')
            safe_install([mgr,'install','-y','-q','-c','conda-forge']+PSI4_EXTRAS)
        psi4_exe=psi4_exe or shutil.which('psi4')
        if not psi4_exe:
            raise RuntimeError('Psi4 installation completed but the psi4 executable was not found on PATH.')
        cmd=[psi4_exe,'-i',INPUT_FILE,'-o',output_file]
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
                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path) as z:
                        for info in z.infolist():
                            name=info.filename.replace('\\','/')
                            norm=os.path.normpath(name)
                            if name.startswith('/') or norm.startswith('..'+os.sep) or norm=='..':
                                raise RuntimeError('Unsafe path in ORCA ZIP archive: '+name)
                        z.extractall(dest)
                    return True
                if tarfile.is_tarfile(archive_path):
                    with tarfile.open(archive_path,'r:*') as t:
                        members=t.getmembers()
                        root=os.path.realpath(dest)
                        for m in members:
                            target=os.path.realpath(os.path.join(dest,m.name))
                            if not (target==root or target.startswith(root+os.sep)):
                                raise RuntimeError('Unsafe path in ORCA tar archive: '+m.name)
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

    send_msg(f"[Kaggle] Starting {'Psi4' if is_psi4 else 'ORCA'} calculation: {INPUT_FILE}")
    timeout_triggered=False
    try:
        if is_psi4:
            proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=remaining_time())
            rc=proc.returncode
            if not os.path.exists(output_file) and proc.stdout:
                Path(output_file).write_text(proc.stdout,encoding='utf-8',errors='replace')
        else:
            with open(output_file,'w',encoding='utf-8',errors='replace') as fout:
                proc=subprocess.run(cmd,stdout=fout,stderr=subprocess.STDOUT,timeout=remaining_time())
                rc=proc.returncode
    except subprocess.TimeoutExpired:
        timeout_triggered=True; rc=124
        send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')

    out_text=Path(output_file).read_text(encoding='utf-8',errors='replace') if os.path.exists(output_file) else ''
    analysis=parse_output_text(out_text,output_file)
    analysis['process_returncode']=rc
    if rc!=0 and not timeout_triggered: send_msg(f'[Kaggle] Program exited with code {rc}. Results will still be analyzed and returned.')

    work='analysis_report'; os.makedirs(work,exist_ok=True)
    plots=make_plots(analysis,work)
    pdf_path=os.path.join(work,basename+'_analysis_report.pdf')
    build_pdf(analysis,plots,pdf_path)
    with open(os.path.join(work,'analysis.json'),'w',encoding='utf-8') as f: json.dump(analysis,f,indent=2,ensure_ascii=False)

    send_msg('[Analysis]\n'+section_summary(analysis))
    for name,path in plots.items():
        try: tg('sendPhoto',{'chat_id':CHAT_ID,'caption':name.replace('_',' ').title()},'photo',path)
        except Exception as e: send_msg('Could not send plot '+name+': '+str(e))
    try: tg('sendDocument',{'chat_id':CHAT_ID,'caption':'Complete scientific analysis PDF'},'document',pdf_path)
    except Exception as e: send_msg('Could not send PDF: '+str(e))

    results_dir='outputs'; os.makedirs(results_dir,exist_ok=True)
    allowed_ext=('.out','.gbw','.xyz','.molden.input','.property.txt','.property.json','.prop','.hess','.interp','.allxyz','.opt','.dat','.cube','.engrad','.trj')
    for parent,dirs,files in os.walk('.'):
        if parent.startswith('./outputs') or parent.startswith('./analysis_report'): continue
        for fn in files:
            if fn==INPUT_FILE or fn.endswith(allowed_ext):
                src=os.path.join(parent,fn)
                if os.path.isfile(src):
                    try: shutil.copy2(src,os.path.join(results_dir,os.path.basename(fn)))
                    except Exception: pass
    archive=shutil.make_archive(('Psi4_Results_' if is_psi4 else 'ORCA_Results_')+basename,'zip',results_dir)
    transfer=send_results_with_fallback(archive,results_dir)
    status='SUCCESS' if (rc==0 and analysis.get('normal_termination')) else ('TIMEOUT' if timeout_triggered else 'FINISHED WITH WARNINGS/ERROR')
    if transfer.get('failed'):
        status += ' | PARTIAL_TRANSFER'
    send_msg('[Kaggle] Final status: '+status)
except Exception as e:
    send_msg('Fatal Kaggle error:\n'+str(e)+'\n'+traceback.format_exc()[-2500:])
'''

def _redact_kaggle_error(text):
    rendered = str(text or '')
    for secret in (KAGGLE_API_TOKEN, KAGGLE_KEY):
        if secret and len(secret) >= 8:
            rendered = rendered.replace(secret, '[REDACTED]')
    return rendered


def _kaggle_cli_command(args, env):
    """Run the Kaggle CLI from THIS Python interpreter/package.

    Render images can contain an older ``kaggle`` console-script on PATH even
    after pip upgrades the package used by this process. Calling that stale
    executable silently drops modern KAGGLE_API_TOKEN authentication. Always
    importing ``kaggle.cli`` through sys.executable keeps the CLI and installed
    package version identical.
    """
    return [
        sys.executable, '-c',
        "import sys; from kaggle.cli import main; sys.argv=['kaggle']+sys.argv[1:]; sys.exit(main())"
    ] + list(args)


def _credential_candidates():
    """Return ordered Kaggle auth candidates without guessing a single mode.

    Precedence:
      1. Anything explicitly supplied in KAGGLE_API_TOKEN is tried as modern auth.
      2. KAGGLE_KEY is tried as legacy username/key auth.
      3. If the API-token value is 32 hex characters, also try it as a legacy
         key as a compatibility fallback. This covers users who stored an old
         kaggle.json key in the newer Render variable name.
    """
    candidates = []
    token = (KAGGLE_API_TOKEN or '').strip()
    key = (KAGGLE_KEY or '').strip()

    if token:
        candidates.append(('modern', token, 'KAGGLE_API_TOKEN'))

    if key:
        candidates.append(('legacy', key, 'KAGGLE_KEY'))

    if token and re.fullmatch(r'[0-9a-fA-F]{32}', token):
        if not any(mode == 'legacy' and cred == token for mode, cred, _src in candidates):
            candidates.append(('legacy', token, 'KAGGLE_API_TOKEN (legacy fallback)'))

    return candidates


def _classify_kaggle_credential():
    """Human-readable summary used only by /start and /version."""
    candidates = _credential_candidates()
    if not candidates:
        return 'none', ''
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1]
    return 'auto-fallback', candidates[0][1]


def _build_kaggle_auth_env(mode, credential):
    """Create one isolated Kaggle auth environment for one candidate."""
    if not KAGGLE_USERNAME:
        raise RuntimeError('KAGGLE_USERNAME is empty. Set your exact Kaggle username in Render.')

    tmp_root = tempfile.mkdtemp(prefix='chembot-kaggle-auth-')
    cfg = os.path.join(tmp_root, '.kaggle')
    os.makedirs(cfg, exist_ok=True)

    env = os.environ.copy()
    env['KAGGLE_CONFIG_DIR'] = cfg
    env['KAGGLE_USERNAME'] = KAGGLE_USERNAME
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'

    if mode == 'modern':
        env['KAGGLE_API_TOKEN'] = credential
        env.pop('KAGGLE_KEY', None)
        token_path = os.path.join(cfg, 'access_token')
        Path(token_path).write_text(credential, encoding='utf-8')
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    elif mode == 'legacy':
        env.pop('KAGGLE_API_TOKEN', None)
        env['KAGGLE_USERNAME'] = KAGGLE_USERNAME
        env['KAGGLE_KEY'] = credential
        legacy_path = os.path.join(cfg, 'kaggle.json')
        Path(legacy_path).write_text(
            json.dumps({'username': KAGGLE_USERNAME, 'key': credential}),
            encoding='utf-8'
        )
        try:
            os.chmod(legacy_path, 0o600)
        except OSError:
            pass
    else:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise RuntimeError('Unknown Kaggle authentication mode: ' + str(mode))

    return tmp_root, env


def _select_working_kaggle_auth():
    """Try each configured credential mode against Kaggle and return the first that works."""
    candidates = _credential_candidates()
    if not candidates:
        raise RuntimeError(
            'No Kaggle credential is configured. Set KAGGLE_API_TOKEN '
            '(recommended) or KAGGLE_KEY, together with KAGGLE_USERNAME.'
        )

    failures = []
    for mode, credential, source in candidates:
        root = None
        try:
            root, env = _build_kaggle_auth_env(mode, credential)
            ok, detail = _kaggle_auth_preflight(env)
            if ok:
                return root, env, mode, source
            failures.append(f'{source} as {mode}: {detail}')
        except Exception as exc:
            failures.append(f'{source} as {mode}: {_redact_kaggle_error(str(exc))[-700:]}')
        if root:
            _safe_rmtree(root)

    raise RuntimeError(
        'Kaggle authentication failed for every configured credential mode.\n'
        + '\n'.join('• ' + f for f in failures)
    )


def _looks_transient_kaggle_error(text):
    low = str(text or '').lower()
    markers = (
        '429', 'too many requests', 'rate limit', '500', '502', '503', '504',
        'service unavailable', 'bad gateway', 'gateway timeout', 'sslerror',
        'max retries exceeded', 'connection reset', 'connection aborted',
        'remote end closed', 'read timed out', 'temporary failure in name resolution'
    )
    return any(m in low for m in markers)


def _kaggle_auth_preflight(env):
    """Return (accepted, diagnostic) using the exact Kaggle runtime CLI."""
    cmd = _kaggle_cli_command(['kernels', 'list', '--page-size', '1'], env)
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60
        )
    except subprocess.TimeoutExpired as exc:
        return False, 'preflight timed out: ' + str(exc)

    combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
    if proc.returncode == 0:
        return True, 'accepted'

    cleaned = _redact_kaggle_error(combined).strip()
    low = cleaned.lower()
    if ('authentication required' in low or 'unauthorized' in low or '401' in low
            or 'forbidden' in low or '403' in low):
        return False, 'credential rejected by Kaggle: ' + cleaned[-650:]
    return False, 'preflight failed: ' + cleaned[-650:]


def submit_kaggle_job(input_name, encoded_files_json, chat_id, is_psi4, extras, drive_link, api_factory=None):
    """Create and push one isolated Kaggle script job using the modern Kaggle CLI auth path."""
    del api_factory  # retained in signature for backward compatibility with older tests/callers
    job_id = 'chem-job-' + uuid.uuid4().hex[:16]
    job_dir = None
    auth_root = None
    try:
        if not KAGGLE_USERNAME:
            raise RuntimeError('KAGGLE_USERNAME is empty. Set your exact Kaggle username in Render.')
        job_dir = tempfile.mkdtemp(prefix=job_id + '_')
        dataset_sources = [] if is_psi4 else [ORCA_DATASET_SLUG]
        metadata = {
            'id': f'{KAGGLE_USERNAME}/{job_id}',
            'title': job_id,
            'code_file': 'script.py',
            'language': 'python',
            'kernel_type': 'script',
            'is_private': True,
            'enable_gpu': False,
            'enable_internet': True,
            'dataset_sources': dataset_sources,
        }
        Path(job_dir, 'kernel-metadata.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        header = (
            'BOT_TOKEN=' + repr(BOT_TOKEN) + '\n'
            'CHAT_ID=' + repr(chat_id) + '\n'
            'INPUT_FILE=' + repr(os.path.basename(input_name)) + '\n'
            'ENCODED_FILES_JSON=' + repr(encoded_files_json) + '\n'
            'DRIVE_LINK=' + repr(drive_link) + '\n'
            'PSI4_EXTRAS=' + repr(extras) + '\n'
        )
        script = header + '\n' + ANALYZER_MODULE_CODE + '\n' + KAGGLE_RUNNER_CODE
        Path(job_dir, 'script.py').write_text(script, encoding='utf-8', newline='\n')

        auth_root, env, auth_mode, auth_source = _select_working_kaggle_auth()
        last_text = ''
        url = None
        for attempt in range(1, 4):
            cmd = _kaggle_cli_command(['kernels', 'push', '-p', job_dir], env)
            try:
                proc = subprocess.run(
                    cmd, env=env, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=240
                )
                combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
            except subprocess.TimeoutExpired as exc:
                proc = None
                combined = f'Kaggle push timed out after 240 seconds: {exc}'
            last_text = combined

            # Current and legacy Kaggle CLIs both print the created notebook URL.
            m = re.search(
                r'https?://(?:www\.)?kaggle\.com/(?:code/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)',
                combined
            )
            if m:
                owner, slug = m.group(1), m.group(2)
                job_id = slug
                url = m.group(0)

            if proc is not None and proc.returncode == 0:
                if not url:
                    url = f'https://www.kaggle.com/code/{KAGGLE_USERNAME}/{job_id}'
                return job_id, url

            # 401/403 and validation errors are permanent; retries cannot fix them.
            low = combined.lower()
            if '401' in low or 'unauthorized' in low or '403' in low or 'forbidden' in low:
                mode = f'{auth_source} as {auth_mode}'
                raise RuntimeError(
                    'Kaggle authentication was rejected (401/403) after preflight while using ' + mode + '. '
                    'Regenerate the corresponding credential in Kaggle Settings > API, update the Render environment variable, and redeploy. '
                    'Do not mix a new API token with an old KAGGLE_KEY.\n' +
                    _redact_kaggle_error(combined)[-1800:]
                )
            if not _looks_transient_kaggle_error(combined) or attempt == 3:
                break
            time.sleep(2 ** attempt)

        detail = _redact_kaggle_error(last_text).strip()[-2200:]
        raise RuntimeError('Kaggle kernels push failed. ' + (detail or 'No diagnostic output was returned.'))
    except Exception as exc:
        raise RuntimeError(f'Kaggle submission failed for {os.path.basename(input_name)}: {exc}') from exc
    finally:
        _safe_rmtree(job_dir)
        _safe_rmtree(auth_root)


# ------------------------- Bot setup -------------------------
# Prune staging debris before Telegram worker threads start.
_stale_removed = cleanup_stale_local_job_dirs()
if _stale_removed:
    print(f"Removed {len(_stale_removed)} stale ChemBot staging director(ies).")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=BOT_WORKER_THREADS)
# Authentication is deliberately deferred to each isolated submission.
# This avoids a stale legacy credential preventing the Render service from starting.
print(f"CHEMBOT v5.4 is initialized with {BOT_WORKER_THREADS} Telegram workers...")


def authorized(message):
    return message.chat.id in ALLOWED_IDS or message.from_user.id in ALLOWED_IDS


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
        ("🌈 TD-DFT / UV-Vis", "uv"), ("🧬 Orbitals", "orb"), ("⚛️ Structure / Charges", "structure"),
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
        plots=make_plots(a,d)
        pdf=os.path.join(d,Path(filename).stem+'_analysis_report.pdf')
        build_pdf(a,plots,pdf)
    with session_lock:
        analysis_sessions[sid]={'chat_id':chat_id,'user_id':user_id,'analysis':a,'dir':d,'plots':plots,'pdf':pdf,'created':time.time()}
        # prune old sessions (>6h)
        for old in list(analysis_sessions):
            if time.time()-analysis_sessions[old]['created']>21600:
                try: shutil.rmtree(analysis_sessions[old]['dir'],ignore_errors=True)
                except Exception: pass
                analysis_sessions.pop(old,None)
    return sid,a


def _recent_analyses(chat_id, user_id, max_age_seconds=600, max_items=10):
    now = time.time()
    with session_lock:
        rows = [x for x in analysis_sessions.values() if x.get('chat_id') == chat_id and x.get('user_id') == user_id and now - x.get('created', 0) <= max_age_seconds]
    rows = sorted(rows, key=lambda x: x.get('created', 0))[-max_items:]
    return rows


def _auto_send_recent_overlays(chat_id, user_id, current_sid):
    rows = _recent_analyses(chat_id, user_id, max_age_seconds=600, max_items=10)
    if len(rows) < 2:
        return
    with session_lock:
        current = analysis_sessions.get(current_sid)
    if not current:
        return
    sent = 0
    for kind, sigma, label in [('uv', 10.0, 'TD-DFT / UV-Vis'), ('ir', 12.0, 'FT-IR')]:
        analyses = [r['analysis'] for r in rows if r['analysis'].get('tddft_states' if kind == 'uv' else 'ir_spectrum')]
        if len(analyses) < 2:
            continue
        out = os.path.join(current['dir'], f'auto_overlay_{kind}.png')
        with render_lock:
            p = make_overlay_plot(analyses, kind, out, normalize=True, sigma=sigma)
        if not p:
            continue
        names = ', '.join(Path(a.get('filename', 'spectrum')).stem for a in analyses)
        caption = f'{label} overlay generated automatically from {len(analyses)} parsed output files.\nFiles: {names}'
        with open(p, 'rb') as f:
            bot.send_photo(chat_id, f, caption=caption[:1020])
        sent += 1
    if sent:
        bot.send_message(chat_id, 'Overlay figures were generated automatically because multiple output files were uploaded within the recent batch window.')


@bot.message_handler(commands=['start'])
def start(message):
    if not authorized(message): return
    uid=message.from_user.id
    with state_lock:
        user_aux_storage.pop(uid,None)
        user_drive_links.pop(uid,None)
    bot.reply_to(message,
        "🧪 Computational Chemistry Bot v5.4\n"
        f"• Kaggle authentication mode: {_classify_kaggle_credential()[0]}\n\n"
        "• Send ORCA .inp or Psi4 .dat to run on Kaggle.\n"
        "• Send ORCA/Psi4 .out for scientific analysis, plots and PDF.\n"
        "• Upload multiple .out files to overlay TD-DFT/UV-Vis or FT-IR spectra.\n"
        "• Send .xyz/.allxyz/.gbw before one or several jobs when needed; the same snapshot is available to the whole batch.\n"
        "• Psi4 D3/D4/gCP/geomeTRIC dependencies are detected and installed automatically.\n"
        "• Use /clearaux after a batch to clear stored auxiliary files/restart URL.\n"
        "• Kaggle sends calculation results directly, so this launcher can be closed after all submissions are confirmed."
    )


@bot.message_handler(commands=['version'])
def version_command(message):
    if not authorized(message): return
    candidates = _credential_candidates()
    candidate_text = ', '.join(f'{src}→{mode} ({len(cred)} chars)' for mode, cred, src in candidates) or 'none'
    bot.reply_to(
        message,
        'ChemBot build: v5.4-KAGGLE-AUTH-FALLBACK-20260926\n'
        f'Kaggle username configured: {"yes" if bool(KAGGLE_USERNAME) else "no"}\n'
        f'Credential candidates: {candidate_text}\n'
        f'ORCA dataset: {ORCA_DATASET_SLUG}'
    )


@bot.message_handler(commands=['help'])
def help_command(message):
    if not authorized(message): return
    bot.reply_to(message,
        "Supported job inputs: .inp (ORCA), .dat (Psi4).\n"
        "Auxiliary/restart inputs: .xyz, .allxyz, .gbw (snapshotted independently into every job in a rapid batch).\n"
        "Analysis input: .out.\n"
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
            elif action=='orb': split_send(call.message.chat.id,section_orbitals(a),'🧬 Molecular orbitals')
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
        try: bot.answer_callback_query(call.id,"Error: "+str(e)[:150],show_alert=True)
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
            bot.send_message(chat_id,section_summary(a),reply_markup=out_menu(sid,a))
        except Exception as e:
            import traceback
            print('Direct .out analysis failure for', original_name)
            traceback.print_exc()
            bot.reply_to(message,'Analysis error: '+str(e))
        return

    # Auxiliary job files
    if filename.endswith(('.xyz','.allxyz','.gbw')):
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
    if filename.endswith(('.inp','.dat')):
        if size>MAX_TELEGRAM_DOWNLOAD:
            bot.reply_to(message,'Input exceeds Telegram Bot API download limit.'); return
        try:
            info=bot.get_file(message.document.file_id); raw=bot.download_file(info.file_path); inp=raw.decode('utf-8',errors='replace')
            is_psi4=filename.endswith('.dat'); extras=detect_psi4_extras(inp) if is_psi4 else []
            prog='Psi4' if is_psi4 else 'ORCA'
            extra_note=("\nDetected external Psi4 packages: "+', '.join(extras)) if extras else ''
            bot.reply_to(message,f"☁️ {prog} input received. Preparing Kaggle job...{extra_note}")

            # Take one immutable context snapshot for this message. Multiple
            # simultaneous .inp/.dat handlers therefore cannot consume each
            # other's auxiliary files or restart URL.
            aux_snapshot, drive = snapshot_user_job_context(uid)

            # Helpful NEB end-point convenience, retaining original behavior.
            if not is_psi4 and 'neb-ts' in inp.lower() and '%neb' not in inp.lower():
                xyzs=[n for n in aux_snapshot if n.lower().endswith(('.xyz','.allxyz'))]
                if len(xyzs)==1:
                    inp += f'\n\n%neb\n  NEB_End_XYZ "{xyzs[0]}"\nend\n'
                    bot.send_message(chat_id,f"NEB-TS endpoint automatically linked: {xyzs[0]}")

            payload={os.path.basename(original_name):base64.b64encode(inp.encode()).decode('ascii')}
            payload.update(aux_snapshot)
            encoded=base64.b64encode(json.dumps(payload).encode()).decode('ascii')
            # submit_kaggle_job uses a UUID slug, a private tempfile directory,
            # a fresh KaggleApi client, and a finally-cleanup for this job only.
            job_id,url=submit_kaggle_job(
                original_name, encoded, chat_id, is_psi4, extras, drive
            )
            bot.send_message(chat_id,f"✅ Kaggle job submitted: {original_name}\nYou may close this launcher after all files in the batch show this confirmation.\n{url}")
        except Exception as e:
            bot.reply_to(message,'Submission error: '+str(e))
        return

    bot.reply_to(message,'Supported files: .inp, .dat, .out, .xyz, .allxyz, .gbw')


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

    print(f"CHEMBOT v5.4 webhook mode active on port {port}.")
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

    print("CHEMBOT v5.4 polling mode active. Ensure no other instance uses this bot token.")
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except telebot.apihelper.ApiTelegramException as exc:
        if getattr(exc, "error_code", None) == 409 or "other getUpdates request" in str(exc):
            raise RuntimeError(
                "Telegram 409 conflict: another process is already polling this bot token. "
                "Stop the other local/Render bot instance, or deploy ChemBot as a Render Web Service "
                "so v5.4 uses webhook mode."
            ) from exc
        raise


if __name__ == '__main__':
    mode = _transport_mode()
    print(f"ChemBot transport selected: {mode}")
    if mode == "webhook":
        run_render_webhook()
    else:
        run_polling()
