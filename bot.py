#!/usr/bin/env python3
"""ChemBot 7.1.2 — final scientific/Telegram/Kaggle script (2026-10-13).

Python 3.10+ (3.11/3.12 recommended). Start: python chembot.py
Render: Web Service, start command python chembot.py, health route /health.
Environment: CHEMBOT_BOT_TOKEN; KAGGLE_USERNAME; KAGGLE_API_TOKEN or KAGGLE_KEY;
Optional owner overrides: CHEMBOT_ALLOWED_IDS=your Telegram numeric user ID;
CHEMBOT_ADMIN_ID=your ID. If omitted, the original bot's trusted IDs are used.
Runtime datasets: ORCA_DATASET_SLUG and GAUSSIAN_DATASET_SLUG (owner/slug).
Engine binaries must already be available to your Kaggle account in those datasets.
Set CHEMBOT_AUTO_INSTALL=0 when dependencies were installed during the build.
Private notebooks contain the delivery token and input; keep them private.
Commands: --help, --self-test, --print-requirements, --analyze file.out --outdir report.
Self-tests use synthetic fixtures and never contact Telegram, Kaggle or engines.
Gaussian setup installs missing tcsh, repairs runtime script line endings, and
preserves installer diagnostics in outputs/gaussian_setup.log.
The parser supports common printed output sections, not every engine protocol.
Reports retain warnings for missing evidence; export formatting does not validate
an unsuitable method, basis, molecule, stationary point or scientific hypothesis.
"""
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
import hmac
from collections import OrderedDict
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ============================================================
# Chemistry Telegram/Kaggle Bot v7.1.2
# ============================================================


def ensure_dependencies(report_only=False):
    packages = {
        'telebot': 'pyTelegramBotAPI>=4.16,<5',
        'matplotlib': 'matplotlib>=3.7,<4',
        'reportlab': 'reportlab>=4.0,<5',
        'numpy': 'numpy>=1.24,<3',
        'requests': 'requests>=2.31,<3',
    }
    if report_only:
        packages.pop('telebot')
        packages.pop('requests')
    allow_install = _clean_env('CHEMBOT_AUTO_INSTALL').lower() not in {'0','no','false'}
    for mod, pkg in packages.items():
        try:
            __import__(mod)
        except ImportError:
            if not allow_install:
                raise RuntimeError('Missing dependency; install ' + pkg + ' during the build step.')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', pkg], timeout=300)

    if report_only:
        return
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
        if not allow_install:
            raise RuntimeError('Install kaggle>=2.2.4,<3 during the build step.')
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', 'kaggle>=2.2.4,<3'
        ], timeout=300)


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


ANALYZER_MODULE_CODE = r'''

from __future__ import annotations

# Scientific analyzer extracted by AST from the five analyzer sources in
# ChemBot v6.2.7, then hardened below. No bot, Kaggle or Telegram startup code.
# This module can run standalone or be embedded in the bot/worker namespace.

import os, re, math, json, textwrap, hashlib
from pathlib import Path

REPORT_GENERATOR_VERSION = '6.2.7'
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
    if e.get('final_energy_hartree') is not None: lines.append(f"Final energy: {e['final_energy_hartree']:.12f} Eh")
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
        'orbitals': orbitals_all,
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
        'frequencies_cm1': freqs,
        'imaginary_frequencies_cm1': imag,
        'expected_frequency_count': expected,
        'stationary_point_status': sp_status,
        'thermochemistry_reliability': thermo_rel,
        'ir_spectrum': ir,
        'tddft_states': td,
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




def conceptual_dft_descriptors(a):
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
    out={'orbitals':orbitals}
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


# ----------------------- scientific parser corrections -----------------------
# Public schema additions:
# warnings/parser_warnings: list[str]; convergence fields: bool | None;
# geometry_units: 'angstrom' | None; source_geometry_units: str | None;
# thermochemistry_blocks and job_blocks retain independent provenance.
BOHR_TO_ANGSTROM = 0.529177210903
FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
PARSER_VERSION = '7.1.2'
REPORT_GENERATOR_VERSION = '7.1.2'
_LEGACY_ANALYZER_PARSE = parse_output_text


def _float(value):
    """Finite numeric values only; missing/unrecognized data are not zero."""
    try:
        result = float(str(value).strip().replace('D', 'E').replace('d', 'e'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _runtime_lines(text):
    """Exclude echoed input cards from runtime status/section recognition."""
    return [line for line in str(text or '').splitlines()
            if not re.match(r'^\s*\|\s*\d+>', line)
            and not re.match(r'^\s*[#!%]', line)]


def _simple_keywords(text):
    groups = []
    for line in str(text or '').splitlines():
        match = re.match(r'^\s*(?:\|\s*\d+>\s*)?!\s*(.+)$', line)
        if match:
            # An ORCA comment begins with #; it cannot name the actual method.
            groups.append(match.group(1).split('#', 1)[0].strip())
    return groups


def _orca_input_method(text):
    """Exact ORCA method token, including parenthesized triples/suffixes."""
    tokens = [token for line in _simple_keywords(text) for token in line.split()]
    patterns = (
        (r'DLPNO-CCSD\(T1\)', 'DLPNO-CCSD(T1)'),
        (r'DLPNO-CCSD\(T\)', 'DLPNO-CCSD(T)'),
        (r'DLPNO-CCSD', 'DLPNO-CCSD'),
        (r'CCSD\(T\)', 'CCSD(T)'), (r'CCSD', 'CCSD'),
        (r'DLPNO-MP2', 'DLPNO-MP2'), (r'RI-?MP2', 'RI-MP2'),
        (r'MP2', 'MP2'), (r'MP3', 'MP3'),
        (r'NEVPT2', 'NEVPT2'), (r'CASSCF', 'CASSCF'),
        (r'CAM-?B3LYP', 'CAM-B3LYP'), (r'B3LYP', 'B3LYP'),
        (r'PBE0', 'PBE0'), (r'M06-?2X', 'M06-2X'),
        (r'M06-?L', 'M06-L'), (r'M06', 'M06'),
        (r'(?:w|ω)B97X-D4', 'wB97X-D4'),
        (r'(?:w|ω)B97X-D3', 'wB97X-D3'),
        (r'(?:w|ω)B97X-D', 'wB97X-D'),
        (r'(?:w|ω)B97X-V', 'wB97X-V'),
        (r'(?:w|ω)B97X', 'wB97X'),
        (r'(?:w|ω)B97M-V', 'wB97M-V'),
        (r'r2SCAN-?3C', 'r2SCAN-3C'), (r'B97-?3C', 'B97-3C'),
        (r'PBEh-?3C', 'PBEh-3C'), (r'HF-?3C', 'HF-3C'),
        (r'B2PLYP', 'B2PLYP'), (r'PWPB95', 'PWPB95'),
        (r'TPSSh', 'TPSSh'), (r'TPSS', 'TPSS'),
        (r'BP86|BP', 'BP86'), (r'BLYP', 'BLYP'),
        (r'revPBE', 'revPBE'), (r'PBE', 'PBE'),
        (r'RHF|UHF|ROHF|HF', 'HF'),
        (r'GFN2-xTB', 'GFN2-xTB'), (r'GFN1-xTB', 'GFN1-xTB'),
        (r'PM3', 'PM3'), (r'AM1', 'AM1'), (r'MNDO', 'MNDO'),
    )
    matches = []
    for token in tokens:
        for pattern, canonical in patterns:
            if re.fullmatch(pattern, token, re.I):
                matches.append(canonical)
                break
    # Conflicting input method tokens need the realized runtime method;
    # selecting the first token is not scientifically defensible.
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _orca_split_jobs(text):
    """Split real run boundaries, never an echoed $new_job/comment."""
    blocks, current = [], []
    observed_result = False
    banner = re.compile(r'^\s*\**\s*O\s{2,}R\s{2,}C\s{2,}A\s*\**\s*$')
    for line in str(text or '').splitlines():
        echoed = bool(re.match(r'^\s*\|\s*\d+>', line))
        boundary = not echoed and (
            bool(banner.match(line)) or bool(re.match(r'^\s*\$new_job\s*$', line, re.I))
        )
        if boundary and current and observed_result:
            blocks.append('\n'.join(current))
            current = []
            observed_result = False
        current.append(line)
        if not echoed and re.search(r'FINAL SINGLE POINT ENERGY|ORCA TERMINATED|CARTESIAN COORDINATES', line, re.I):
            observed_result = True
    if current:
        blocks.append('\n'.join(current))
    return [block for block in blocks if block.strip()]


def _strict_status(text, engine):
    runtime = '\n'.join(_runtime_lines(text))
    if engine == 'ORCA':
        success = r'^\s*\**\s*ORCA\s+TERMINATED\s+NORMALLY\b'
        failure = r'ORCA\s+(?:TERMINATED\s+ABNORMALLY|finished\s+by\s+error\s+termination)|INPUT\s+ERROR|ABORTING\s+THE\s+RUN|SEGMENTATION\s+FAULT|OUT\s+OF\s+MEMORY|UNRECOGNIZED\s+OR\s+DUPLICATED\s+KEYWORD'
    elif engine == 'Psi4':
        success = r'^\s*\**\s*Psi4\s+exiting\s+successfully\b'
        failure = r'PSIEXCEPTION|TRACEBACK|FATAL\s+ERROR|SEGMENTATION\s+FAULT|OUT\s+OF\s+MEMORY|CONVERGENCE\s+FAILURE|SCFConvergenceError|OptimizationConvergenceError'
    else:
        return None, []
    markers = [(match.start(), True) for match in re.finditer(success, runtime, re.I | re.M)]
    markers.extend((match.start(), False) for match in re.finditer(failure, runtime, re.I | re.M))
    status = max(markers, key=lambda item: item[0])[1] if markers else None
    errors = [line.strip() for line in runtime.splitlines() if re.search(failure, line, re.I)]
    return status, errors


def _last_status_flag(runtime, success, failure):
    marks = [(match.start(), True) for match in re.finditer(success, runtime, re.I | re.M)]
    marks += [(match.start(), False) for match in re.finditer(failure, runtime, re.I | re.M)]
    return max(marks, key=lambda item: item[0])[1] if marks else None


def _convergence_flags(text, engine):
    runtime = '\n'.join(_runtime_lines(text))
    if engine == 'ORCA':
        keywords = [word.lower() for group in _simple_keywords(text) for word in group.split()]
        optimization_words = {'opt', 'tightopt', 'verytightopt', 'looseopt', 'optts', 'copt', 'neb-ts', 'zoom-neb-ts', 'neb', 'neb-ci'}
        requested = True if any(word in optimization_words for word in keywords) else (False if keywords else None)
        if re.search(r'GEOMETRY OPTIMIZATION|THE OPTIMIZATION HAS CONVERGED|optimization did not converge', runtime, re.I):
            requested = True
        optimized = _last_status_flag(
            runtime, r'THE\s+OPTIMIZATION\s+HAS\s+CONVERGED',
            r'optimization\s+did\s+not\s+converge|optimization\s+(?:failed|has\s+failed)|MAXIMUM\s+NUMBER\s+OF\s+OPTIMIZATION\s+CYCLES\s+(?:REACHED|EXCEEDED)')
        scf = _last_status_flag(
            runtime, r'\bSCF\s+CONVERGED\s+(?:AFTER|IN)\s+\d+\s+(?:CYCLES|ITERATIONS)',
            r'\bSCF\s+(?:NOT\s+CONVERGED|DID\s+NOT\s+CONVERGE|FAILED\s+TO\s+CONVERGE)|SCF\s+CONVERGENCE\s+FAILURE')
    else:
        requested = None
        if re.search(r'\b(?:optimize|opt)\s*\(|OptKing|Geometry\s+Optimization|Optimization\s+is\s+complete', text, re.I):
            requested = True
        elif re.search(r'\b(?:energy|frequency|frequencies)\s*\(', text, re.I):
            requested = False
        optimized = _last_status_flag(
            runtime, r'Optimization\s+is\s+complete|Geometry\s+(?:optimization\s+)?converged|Optimization\s+converged',
            r'OptimizationConvergenceError|optimization\s+(?:did\s+not\s+converge|failed)|maximum\s+(?:number\s+of\s+)?(?:geometry\s+|optimization\s+)?iterations\s+(?:exceeded|reached)')
        scf = _last_status_flag(
            runtime, r'Energy\s+and\s+wave\s*function\s+converged\.?|^\s*Energy\s+converged\.',
            r'SCFConvergenceError|SCF\s+(?:failed\s+to\s+converge|did\s+not\s+converge|not\s+converged)|Could\s+not\s+converge\s+SCF')
    return {'optimization_requested': requested, 'optimization_converged': optimized, 'scf_converged': scf}


def _orca_geometry_strict(text):
    """Latest complete printed coordinate table; source units retained."""
    blocks, current, unit, active = [], [], None, False
    for line in _runtime_lines(text):
        header = re.search(r'^\s*CARTESIAN\s+COORDINATES\s*\(([^)]+)\)', line, re.I)
        if header:
            if current:
                blocks.append((current, unit))
            current = []
            spelling = header.group(1).strip().upper()
            unit = 'angstrom' if spelling in ('ANGSTROEM', 'ANGSTROM') else ('bohr' if spelling in ('A.U.', 'AU', 'BOHR') else None)
            active = True
            continue
        if not active:
            continue
        pieces = line.split()
        atom = None
        if len(pieces) >= 4:
            coordinates = [_float(value) for value in pieces[-3:]]
            if all(value is not None for value in coordinates):
                for label in pieces[:-3]:
                    if re.fullmatch(r'[A-Za-z]{1,3}:?', label):
                        factor = BOHR_TO_ANGSTROM if unit == 'bohr' else 1.0
                        atom = dict(element=label.rstrip(':').capitalize() + (':' if label.endswith(':') else ''),
                                    x=coordinates[0] * factor, y=coordinates[1] * factor, z=coordinates[2] * factor)
                        break
        if atom:
            current.append(atom)
        elif current:
            blocks.append((current, unit))
            current = []
            active = False
    if current:
        blocks.append((current, unit))
    if not blocks:
        return [], None, None
    geometry, source_unit = blocks[-1]
    return geometry, ('angstrom' if source_unit in ('angstrom', 'bohr') else None), source_unit


def _orca_frequency_table(text):
    blocks, current, active = [], [], False
    row = re.compile(r'^\s*(\d+)\s*:\s*(' + FLOAT_RE + r')\s*(?:cm\*\*-1|cm\^-1|cm-1)?(.*)$', re.I)
    for line in _runtime_lines(text):
        if re.match(r'^\s*(?:3N-[56]\s+)?VIBRATIONAL\s+FREQUENCIES\s*$', line, re.I):
            if current:
                blocks.append(current)
            current = []
            active = True
            continue
        if not active:
            continue
        match = row.match(line)
        if match:
            value = _float(match.group(2))
            if value is not None:
                if 'imaginary' in match.group(3).lower() and value > 0:
                    value = -value
                current.append({'mode': int(match.group(1)), 'frequency_cm1': value})
        elif current and line.strip() and set(line.strip()) - set('-='):
            blocks.append(current)
            current, active = [], False
    if current:
        blocks.append(current)
    return blocks[-1] if blocks else []


def _orca_spectrum_table(text, kind):
    """Bounded IR/Raman tables with named columns; no thermochemistry bleed."""
    blocks, current, active, columns = [], [], False, None
    row = re.compile(r'^\s*(\d+)\s*:\s*(.*)$')
    for line in _runtime_lines(text):
        if re.match(r'^\s*' + kind + r'\s+SPECTRUM\s*$', line, re.I):
            if current:
                blocks.append(current)
            current, columns, active = [], None, True
            continue
        if not active:
            continue
        upper = line.upper()
        if 'MODE' in upper and 'FREQ' in upper:
            columns = {'intensity': 2 if re.search(r'\bEPS\b', upper) else 1} if kind == 'IR' else ({'activity': 1, 'depolarization': 2} if 'ACTIVITY' in upper else None)
            continue
        match = row.match(line)
        if match:
            values = [_float(value) for value in re.findall(FLOAT_RE, match.group(2))]
            if not values or values[0] is None:
                continue
            record = {'mode': int(match.group(1)), 'frequency_cm1': values[0]}
            if kind == 'IR':
                pos = columns['intensity'] if columns else None
                record['intensity_km_mol'] = values[pos] if pos is not None and len(values) > pos else None
            else:
                pos = columns['activity'] if columns else None
                record['activity_angstrom4_amu'] = values[pos] if pos is not None and len(values) > pos else None
                record['depolarization_ratio'] = values[2] if columns and len(values) > 2 else None
            current.append(record)
            continue
        if current and line.strip() and set(line.strip()) - set('-='):
            blocks.append(current)
            current, active = [], False
    if current:
        blocks.append(current)
    return blocks[-1] if blocks else []


def parse_raman(text):
    return [(record['frequency_cm1'], record['activity_angstrom4_amu'])
            for record in _orca_spectrum_table(text, 'RAMAN')
            if record.get('activity_angstrom4_amu') is not None]


def _psi4_orbitals_v59(text):
    """Parse Psi4 symmetry labels without reading label digits as energies."""
    current, tables, active = [], [], False
    spin, occupancy = 'restricted', None
    orbital_factor = 1.0
    table_header = re.compile(r'^\s*(?:==>\s*)?Orbital\s+Energies(?:\s*\[[^]]*\])?\s*(?:<==)?\s*$', re.I)
    occupied_header = re.compile(r'^\s*(?:(Alpha|Beta)\s+)?(Doubly\s+Occupied|Singly\s+Occupied|Occupied|Virtual)\s*:\s*(.*)$', re.I)
    pair = re.compile(r'(?<!\S)(\d+[A-Za-z][A-Za-z0-9\'"+\-]*|\d+)\s+(' + FLOAT_RE + r')(?=\s|$)')
    explicit_spin = False
    for line in _runtime_lines(text):
        if table_header.match(line):
            if current:
                tables.append(current)
            current, active, spin, occupancy = [], True, 'restricted', None
            explicit_spin = False
            units = re.search(r'\[([^]]+)\]', line)
            unit = units.group(1).strip().lower() if units else 'eh'
            orbital_factor = 1.0 if unit in ('eh', 'hartree', 'a.u.', 'au') else (1.0/HARTREE_TO_EV if unit == 'ev' else None)
            continue
        if not active:
            continue
        match = occupied_header.match(line)
        data = line
        if match:
            if match.group(1):
                spin = match.group(1).lower()
                explicit_spin = True
            label = match.group(2).upper()
            occupancy = 0.0 if label == 'VIRTUAL' else (1.0 if explicit_spin or label == 'SINGLY OCCUPIED' else 2.0)
            data = match.group(3)
        elif re.match(r'^\s*(Alpha|Beta)\s+(?:Orbitals|Orbital\s+Energies)\s*:?\s*$', line, re.I):
            spin = re.match(r'^\s*(Alpha|Beta)', line, re.I).group(1).lower()
            explicit_spin = True
            occupancy = None
            continue
        if occupancy is None:
            continue
        matches = list(pair.finditer(data))
        consumed = pair.sub('', data).strip()
        if matches and not consumed:
            for value in matches:
                energy = _float(value.group(2))
                if energy is None or orbital_factor is None:
                    continue
                energy *= orbital_factor
                label = value.group(1)
                symmetry = re.sub(r'^\d+', '', label) or None
                index = int(re.match(r'\d+', label).group(0))
                current.append({'index': index, 'label': label, 'symmetry': symmetry,
                                'spin': spin, 'occ': occupancy, 'eh': energy, 'ev': energy * HARTREE_TO_EV})
            continue
        # Blank separation after Virtual: must not close the table: Psi4
        # prints the actual orbital rows below an empty line.
        if not data.strip() or set(data.strip()) <= set('-=') or match:
            continue
        if current:
            tables.append(current)
        current, active, occupancy = [], False, None
    if current:
        tables.append(current)
    return _frontiers_complete(tables[-1] if tables else [])


def _parse_psi4_vibrations(text):
    """Keep column alignment when a Psi4 frequency/intensity cell is missing."""
    frequencies, intensities, pending = [], [], []
    for line in _runtime_lines(text):
        if re.search(r'Freq\s*\[cm\^-1\]', line, re.I):
            pending = []
            for token in line.split(']', 1)[-1].split():
                imaginary = token.lower().endswith('i')
                value = _float(token[:-1] if imaginary else token)
                if value is not None and imaginary:
                    value = -abs(value)
                pending.append(value)
                if value is not None:
                    frequencies.append(value)
        elif pending and re.search(r'IR\s+activ\s*\[km/mol\]', line, re.I):
            values = [_float(token) for token in line.split(']', 1)[-1].split()]
            intensities.extend((frequency, value) for frequency, value in zip(pending, values)
                               if frequency is not None and value is not None)
            pending = []
    return frequencies, intensities


def _frontiers_complete(orbitals):
    """Compute each spin channel from all parsed orbitals, without trimming."""
    output = {'orbitals': list(orbitals), 'homo_ev': None, 'lumo_ev': None, 'gap_ev': None}
    for spin in ('restricted', 'alpha', 'beta'):
        rows = [record for record in orbitals if record.get('spin', 'restricted') == spin]
        occupied = [record for record in rows if _float(record.get('occ')) is not None and record['occ'] > 1e-8 and _float(record.get('ev')) is not None]
        virtual = [record for record in rows if _float(record.get('occ')) is not None and record['occ'] <= 1e-8 and _float(record.get('ev')) is not None]
        h = max((record['ev'] for record in occupied), default=None)
        l = min((record['ev'] for record in virtual), default=None)
        if spin != 'restricted':
            output[spin + '_homo_ev'] = h
            output[spin + '_lumo_ev'] = l
            output[spin + '_gap_ev'] = l - h if h is not None and l is not None else None
    occupied = [record['ev'] for record in orbitals if _float(record.get('occ')) is not None and record['occ'] > 1e-8 and _float(record.get('ev')) is not None]
    virtual = [record['ev'] for record in orbitals if _float(record.get('occ')) is not None and record['occ'] <= 1e-8 and _float(record.get('ev')) is not None]
    output['homo_ev'] = max(occupied, default=None)
    output['lumo_ev'] = min(virtual, default=None)
    if output['homo_ev'] is not None and output['lumo_ev'] is not None:
        output['gap_ev'] = output['lumo_ev'] - output['homo_ev']
    return output


def _thermo_segments(text, engine):
    runtime = '\n'.join(_runtime_lines(text))
    if engine == 'ORCA':
        header = r'^\s*THERMOCHEMISTRY(?:\s+AT\b[^\n]*)?\s*$'
    else:
        header = r'^\s*(?:==>\s*)?Thermochemistry(?:\s+Components|\s+at\b[^\n]*)?\s*(?:<==)?\s*$'
    starts = [match.start() for match in re.finditer(header, runtime, re.I | re.M)]
    if not starts:
        # Recognized isolated totals can still be extracted, but their
        # geometry/method association remains unknown.
        return [(0, runtime)]
    return [(start, runtime[start:(starts[index+1] if index+1 < len(starts) else len(runtime))]) for index, start in enumerate(starts)]


def _thermo_pressure(text):
    result = None
    for match in re.finditer(r'\bPressure\s*(?:\.+|[:=])\s*(' + FLOAT_RE + r')\s*\[?(atm|bar|Pa|kPa)\]?', text, re.I):
        value = _float(match.group(1))
        unit = match.group(2).lower()
        if value is not None:
            result = value * {'atm': 1.0, 'bar': 100000/101325, 'pa': 1/101325, 'kpa': 1000/101325}[unit]
    return result


def _orca_thermo_block(text, reference=None):
    f = FLOAT_RE
    patterns = {
        'temperature_K': [r'\bTemperature\s*(?:\.+|[:=])\s*(' + f + r')\s*K', r'THERMOCHEMISTRY\s+AT\s+(' + f + r')\s*K'],
        'electronic_reference_hartree': [r'\bElectronic\s+energy\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'zpe_hartree': [r'\b(?:(?:Non-thermal|Total)\s+)?Zero[ -]point\s+(?:vibrational\s+)?energy\s*(?:\.+|[:=])?\s*(' + f + r')\s*Eh'],
        'thermal_energy_hartree': [r'\bTotal\s+thermal\s+energy\s*(?:\.+|[:=])?\s*(' + f + r')\s*Eh'],
        'total_internal_energy_correction_hartree': [r'^\s*Total\s+correction\s*(?:\.+|[:=])?\s*(' + f + r')\s*Eh'],
        'thermal_energy_correction_hartree': [r'\bThermal\s+energy\s+correction\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'enthalpy_hartree': [r'\b(?:Total|Final)(?:\s+thermal)?\s+enthalpy\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'pv_correction_hartree': [r'\bThermal\s+enthalpy\s+correction\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'gibbs_hartree': [r'\b(?:Final|Total)\s+Gibbs\s+(?:free\s+)?(?:energy|enthalpy)\s*(?:\.+|[:=])?\s*(' + f + r')\s*Eh'],
        'gibbs_correction_hartree': [r'\bThermal\s+(?:Gibbs\s+)?free\s+energy\s+correction\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'entropy_term_hartree': [r'\bFinal\s+entropy\s+term\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'entropy_correction_hartree': [r'\bTotal\s+entropy\s+correction\s*(?:\.+|[:=])\s*(' + f + r')\s*Eh'],
        'rrho_cutoff_cm1': [r'Frequency\s+cutoff\s+for\s+the\s+RRHO\s*(?:\.+|[:=])\s*(' + f + r')\s*cm\*\*-1'],
    }
    result = {key: _last_number(text, values) for key, values in patterns.items()}
    result['pressure_atm'] = _thermo_pressure(text)
    if result['electronic_reference_hartree'] is None:
        result['electronic_reference_hartree'] = reference
        result['reference_energy_source'] = 'preceding electronic-energy record' if reference is not None else None
    else:
        result['reference_energy_source'] = 'printed thermochemistry electronic energy'
    ref = result['electronic_reference_hartree']
    if result['thermal_energy_hartree'] is None and ref is not None and result['total_internal_energy_correction_hartree'] is not None:
        result['thermal_energy_hartree'] = ref + result['total_internal_energy_correction_hartree']
        result['thermal_energy_hartree_source'] = 'electronic reference plus printed total U correction (including ZPE)'
    # In ORCA the printed Thermal Enthalpy correction is pV = RT.
    # H = U + RT, not E_el + RT. Never omit ZPE/thermal contributions.
    if (result['enthalpy_hartree'] is None and result['thermal_energy_hartree'] is not None
            and result['pv_correction_hartree'] is not None):
        result['enthalpy_hartree'] = result['thermal_energy_hartree'] + result['pv_correction_hartree']
        result['enthalpy_hartree_source'] = 'printed total thermal energy plus printed pV correction'
    if result['gibbs_hartree'] is None and result['enthalpy_hartree'] is not None and result['entropy_term_hartree'] is not None:
        result['gibbs_hartree'] = result['enthalpy_hartree'] - result['entropy_term_hartree']
        result['gibbs_hartree_source'] = 'printed/verified enthalpy minus printed TS term'
    # Give shared correction fields the same H-E_el / G-E_el meaning used
    # for Gaussian and Psi4; keep ORCA's printed RT separately as pV.
    result['enthalpy_correction_hartree'] = (result['enthalpy_hartree'] - ref
        if result['enthalpy_hartree'] is not None and ref is not None else None)
    if result['gibbs_hartree'] is not None and ref is not None:
        result['gibbs_correction_hartree'] = result['gibbs_hartree'] - ref
    contributions = []
    for name in ('vib', 'rot', 'trans', 'elec'):
        key = 'S_' + name + '_cal_mol_K'
        value = _last_number(text, [r'\bS_' + name + r'\s*(?:\.+|[:=])\s*(' + f + r')\s*cal/mol-K'])
        result[key] = value
        contributions.append(value)
    if all(value is not None for value in contributions):
        result['entropy_J_mol_K'] = sum(contributions) * 4.184
    elif all(result.get(key) is not None for key in ('enthalpy_hartree', 'gibbs_hartree', 'temperature_K')) and result['temperature_K'] > 0:
        result['entropy_J_mol_K'] = (result['enthalpy_hartree'] - result['gibbs_hartree']) * HARTREE_TO_KJMOL * 1000 / result['temperature_K']
    else:
        result['entropy_J_mol_K'] = None
    return result


def _strict_thermochemistry(text, engine):
    runtime = '\n'.join(_runtime_lines(text))
    blocks = []
    for index, (offset, segment) in enumerate(_thermo_segments(text, engine), 1):
        preceding = runtime[:offset]
        if engine == 'ORCA':
            reference = _last_number(preceding, [r'FINAL SINGLE POINT ENERGY\s+(' + FLOAT_RE + r')'])
            block = _orca_thermo_block(segment, reference)
            block['thermo_method'] = _orca_input_method(text)
            block['thermo_method_source'] = 'explicit input method in the same job' if block['thermo_method'] is not None else None
            basis_match = re.findall(r'Your calculation utilizes the basis:\s*\n?\s*([^\n]+)', preceding, re.I)
            block['thermo_basis'] = basis_match[-1].strip() if basis_match else None
        else:
            reference, label = _psi4_final_energy_v64(preceding)
            # Explicit totals are authoritative. A preceding correlated
            # single point is not silently combined with SCF corrections.
            block = _psi4_thermo_v59(segment, reference if label == 'SCF' else None)
            block['electronic_reference_hartree'] = reference if label == 'SCF' else None
            block['reference_energy_source'] = 'preceding SCF electronic-energy record' if label == 'SCF' else None
            block['pressure_atm'] = _thermo_pressure(segment)
            thermo_method, thermo_basis, _ = _psi4_method_basis_v59(preceding)
            block['thermo_method'] = thermo_method if label == 'SCF' else None
            block['thermo_basis'] = thermo_basis if label == 'SCF' else None
        block['electronic_energy_hartree'] = block.get('electronic_reference_hartree')
        treatment = re.search(r'(quasi[ -]?RRHO(?:\s+method\s+of\s+Grimme)?|Grimme[ -]?quasi[ -]?RRHO|Standard[ -]RRHO|qRRHO|\bRRHO\b|harmonic\s+oscillator\s+approximation)', segment, re.I)
        block['thermo_treatment'] = treatment.group(1) if treatment else None
        block['block_index'] = index
        for key in ('zpe_hartree', 'enthalpy_hartree', 'gibbs_hartree', 'thermal_energy_hartree', 'temperature_K', 'pressure_atm', 'entropy_J_mol_K'):
            block.setdefault(key, None)
        for key in list(block):
            if key.endswith('_hartree') and _float(block[key]) is not None:
                block[key.replace('_hartree', '_kj_mol')] = block[key] * HARTREE_TO_KJMOL
        if any(block.get(key) is not None for key in ('zpe_hartree', 'enthalpy_hartree', 'gibbs_hartree', 'thermal_energy_hartree', 'temperature_K')):
            blocks.append(block)
    primary = dict(blocks[-1]) if blocks else {key: None for key in ('zpe_hartree', 'enthalpy_hartree', 'gibbs_hartree', 'thermal_energy_hartree', 'temperature_K', 'pressure_atm', 'thermo_treatment', 'thermo_method', 'thermo_basis', 'entropy_J_mol_K', 'electronic_reference_hartree', 'electronic_energy_hartree')}
    return primary, blocks


def _orca_protocol(text, analysis):
    requested = _orca_input_method(text)
    groups = _simple_keywords(text)
    tokens = [word for group in groups for word in group.split()]
    for token in tokens:
        solvent_card = re.fullmatch(r'CPCM\(([^)]+)\)', token, re.I)
        if solvent_card:
            analysis['solvation_model'] = analysis.get('solvation_model') or 'CPCM'
            analysis['solvent'] = analysis.get('solvent') or solvent_card.group(1)
    if re.search(r'(?im)^\s*(?:\|\s*\d+>\s*)?%cpcm\b|CPCM\s+SOLVATION\s+MODEL', text):
        analysis['solvation_model'] = analysis.get('solvation_model') or 'CPCM'
    if re.search(r'SMD\s+SOLVATION\s+MODEL|(?im:^\s*(?:\|\s*\d+>\s*)?smd\s+true\b)', text, re.I):
        analysis['solvation_model'] = 'SMD'
    dispersion = None
    for token in tokens:
        if re.fullmatch(r'D3BJ|D3ZERO|D3|D4|D3M|D3MBJ|NLC|VV10', token, re.I):
            dispersion = token.upper()
    if dispersion is None and requested:
        for suffix, label in [('-D4', 'D4'), ('-D3', 'D3'), ('-V', 'VV10')]:
            if requested.upper().endswith(suffix):
                dispersion = label
                break
    if dispersion is None:
        explicit = re.search(r'(?:Dispersion\s+correction|vdW-correction)\s*(?:\.+|[:=])\s*(D3BJ|D3ZERO|D3|D4|VV10)\b', text, re.I)
        if explicit:
            dispersion = explicit.group(1).upper()
    analysis['method_requested'] = requested
    # Retain resolved output components independently of the requested name.
    analysis['method'] = requested or analysis.get('method')
    analysis['dispersion'] = dispersion
    analysis['raw_simple_keywords'] = groups
    analysis['calculation_protocol'] = {
        'method': analysis.get('method'), 'basis': analysis.get('basis'),
        'dispersion': dispersion, 'solvation_model': analysis.get('solvation_model'),
        'solvent': analysis.get('solvent'), 'raw_simple_keywords': groups,
        'method_components': analysis.get('method_components') or {},
    }


def _orca_td_table(text):
    """Electric-dipole transitions only, with original state identities."""
    tables, current, active = [], [], False
    new_row = re.compile(r'^\s*(\S+)\s+->\s+(\S+)\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')(?:\s|$)')
    old_row = re.compile(r'^\s*(\d+)\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')(?:\s|$)')
    header = re.compile(r'^\s*ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS\s*$', re.I)
    for line in _runtime_lines(text):
        if header.match(line):
            if current:
                tables.append(current)
            current, active = [], True
            continue
        if not active:
            continue
        match = new_row.match(line)
        if match:
            source, target = match.group(1, 2)
            ev, cm1, printed_nm, strength = [_float(match.group(index)) for index in range(3, 7)]
            target_index = re.match(r'\d+', target)
            state = int(target_index.group(0)) if target_index else None
        else:
            match = old_row.match(line)
            if match:
                state = int(match.group(1))
                cm1, printed_nm, strength = [_float(match.group(index)) for index in range(2, 5)]
                ev = cm1 / 8065.544005 if cm1 is not None else None
                source, target = None, str(state)
        if match:
            if ev is not None and ev > 0 and cm1 is not None and cm1 > 0 and strength is not None and strength >= 0:
                current.append({'state': state, 'initial_state': source, 'final_state': target,
                                'ev': ev, 'cm1': cm1, 'nm': 1239.841984 / ev,
                                'printed_nm': printed_nm, 'f': strength,
                                'quantity': 'oscillator strength', 'gauge': 'electric dipole'})
            continue
        if current and line.strip() and set(line.strip()) - set('-='):
            tables.append(current)
            current, active = [], False
    if current:
        tables.append(current)
    return tables[-1] if tables else []


def _optimization_trace(text, engine):
    """Recognized optimization window only; no frequency displacement SPs."""
    active, values = False, []
    for line in _runtime_lines(text):
        if re.search(r'GEOMETRY OPTIMIZATION (?:CYCLE|RUN)|Optimization\s+(?:Step|Iteration)|OptKing', line, re.I):
            active = True
        if re.search(r'THE OPTIMIZATION HAS CONVERGED|Optimization is complete|VIBRATIONAL FREQUENCIES|Thermochemistry|optimization did not converge', line, re.I):
            active = False
        if not active:
            continue
        pattern = r'FINAL SINGLE POINT ENERGY\s+(' + FLOAT_RE + r')' if engine == 'ORCA' else r'@(?:DF-)?(?:RHF|UHF|ROHF|RKS|UKS|SCF)\s+Final Energy:\s*(' + FLOAT_RE + r')'
        match = re.search(pattern, line, re.I)
        if match:
            value = _float(match.group(1))
            if value is not None:
                values.append(value)
    return values


def _psi4_split_jobs(text):
    """Separate concatenated program runs without combining independent results."""
    starts = [m.start() for m in re.finditer(
        r'^\s*Psi4\s*:\s*An\s+Open[- ]Source\s+Ab\s+Initio', text, re.I | re.M)]
    if len(starts) < 2:
        return [str(text or '')]
    starts[0] = 0
    return [text[start:(starts[i+1] if i+1 < len(starts) else len(text))]
            for i, start in enumerate(starts)]


def _psi4_geometry_strict(text):
    """Last printed Cartesian table, with explicit Angstrom/Bohr evidence."""
    tables, rows, active, unit = [], [], False, None
    row = re.compile(r'^\s*(?:\d+\s+)?([A-Za-z]{1,2})\s+(' + FLOAT_RE + r')\s+('
                     + FLOAT_RE + r')\s+(' + FLOAT_RE + r')\s*$')
    for line in _runtime_lines(text):
        if re.search(r'\b(?:Cartesian\s+)?Geometry\s*\(in\s+[^)]+\)|Final optimized geometry', line, re.I):
            if rows:
                tables.append((rows, unit))
            rows, active, unit = [], True, None
            u = re.search(r'\(in\s+(Angstroms?|Bohr|a\.u\.)\)', line, re.I)
            if u:
                unit = 'angstrom' if u.group(1).lower().startswith('angstrom') else 'bohr'
            continue
        if not active:
            continue
        units = re.match(r'^\s*units\s+(angstroms?|bohr|au|a\.u\.)\b', line, re.I)
        if units:
            unit = 'angstrom' if units.group(1).lower().startswith('angstrom') else 'bohr'
            continue
        match = row.match(line)
        if match:
            xyz = [_float(match.group(i)) for i in (2, 3, 4)]
            if all(value is not None for value in xyz):
                rows.append(dict(element=match.group(1).capitalize(), x=xyz[0], y=xyz[1], z=xyz[2]))
        elif rows and line.strip() and not re.match(r'^\s*[{}]', line):
            tables.append((rows, unit))
            rows, active = [], False
    if rows:
        tables.append((rows, unit))
    if not tables:
        return [], None, None
    geometry, unit = tables[-1]
    if unit == 'bohr':
        for atom in geometry:
            for axis in ('x', 'y', 'z'):
                atom[axis] *= BOHR_TO_ANGSTROM
    return geometry, ('angstrom' if unit else None), unit


def _dipole_strict(text, engine):
    """Do not label an atomic-unit vector/magnitude as Debye."""
    result = {}
    if engine == 'ORCA':
        matches = list(re.finditer(r'^\s*Total\s+Dipole\s+Moment\s*:\s*(' + FLOAT_RE
                                  + r')\s+(' + FLOAT_RE + r')\s+(' + FLOAT_RE + r')', text, re.I | re.M))
        if matches:
            result['vector_au'] = [_float(matches[-1].group(i)) for i in (1, 2, 3)]
            result['vector'] = [v * 2.541746473 for v in result['vector_au']]
            result['vector_units'] = 'debye'
            result['vector_source'] = 'ORCA Total Dipole Moment vector in atomic units'
        magnitude = _last_number(text, [r'Magnitude\s*\(\s*Debye\s*\)\s*:\s*(' + FLOAT_RE + r')'])
        if magnitude is not None:
            result['magnitude_debye'] = magnitude
        elif result.get('vector'):
            result['magnitude_debye'] = math.sqrt(sum(v*v for v in result['vector']))
            result['magnitude_source'] = 'norm of converted printed atomic-unit vector'
    elif engine == 'Psi4':
        headers = list(re.finditer(r'^\s*Dipole\s+Moment\s*:?\s*(?:\[([^]]+)\]|\(([^)]+)\))', text, re.I | re.M))
        for header in headers:
            unit = (header.group(1) or header.group(2) or '').strip().lower()
            factor = 1.0 if unit in ('d', 'debye') else (2.541746473 if unit in ('e a0', 'e a_0', 'a.u.', 'au') else None)
            if factor is None:
                continue
            segment = text[header.end():header.end()+500]
            match = re.search(r'X\s*[:=]\s*(' + FLOAT_RE + r')\s+Y\s*[:=]\s*('
                              + FLOAT_RE + r')\s+Z\s*[:=]\s*(' + FLOAT_RE + r')', segment, re.I)
            if not match:
                match = re.search(r'\bX\s+Y\s+Z\s+Total\s*\n\s*(' + FLOAT_RE + r')\s+('
                                  + FLOAT_RE + r')\s+(' + FLOAT_RE + r')', segment, re.I)
            if match:
                vector = [_float(match.group(i)) for i in (1, 2, 3)]
                if all(v is not None for v in vector):
                    vector = [v * factor for v in vector]
                    result = {'vector': vector, 'vector_units': 'debye',
                              'magnitude_debye': math.sqrt(sum(v*v for v in vector)),
                              'source_units': unit, 'magnitude_source': 'norm of printed vector with confirmed units'}
    return result


def _parse_one_scientific(text, filename, engine):
    analysis = _LEGACY_ANALYZER_PARSE(text, filename)
    warnings = list(analysis.get('warnings') or [])
    analysis['parser_version'] = PARSER_VERSION
    analysis['normal_termination'], analysis['errors'] = _strict_status(text, engine)
    analysis.update(_convergence_flags(text, engine))
    if engine == 'ORCA':
        analysis['final_geometry'], analysis['geometry_units'], analysis['source_geometry_units'] = _orca_geometry_strict(text)
        modes = _orca_frequency_table(text)
        analysis['frequency_modes'] = modes
        analysis['freq_layout'] = '3N_WITH_RIGID' if modes else None
        analysis['frequency_layout'] = analysis['freq_layout']
        analysis['frequencies_cm1'] = [mode['frequency_cm1'] for mode in modes]
        analysis['imaginary_frequencies_cm1'] = [value for value in analysis['frequencies_cm1'] if value < 0]
        ir = _orca_spectrum_table(text, 'IR')
        raman = _orca_spectrum_table(text, 'RAMAN')
        analysis['ir_modes'], analysis['raman_modes'] = ir, raman
        analysis['ir_spectrum'] = [(record['frequency_cm1'], record['intensity_km_mol']) for record in ir if record.get('intensity_km_mol') is not None]
        analysis['raman_spectrum'] = [(record['frequency_cm1'], record['activity_angstrom4_amu']) for record in raman if record.get('activity_angstrom4_amu') is not None]
        analysis['raman_quantity'] = 'scattering activity'
        analysis['raman_units'] = 'angstrom^4/amu'
        analysis['tddft_states'] = _orca_td_table(text)
        if any(record.get('intensity_km_mol') is None for record in ir):
            warnings.append('Some IR table rows have no confirmed intensity column; these rows are excluded from the spectrum.')
        if any(record.get('activity_angstrom4_amu') is None for record in raman):
            warnings.append('Some Raman table rows have no confirmed activity column; these rows are excluded from the spectrum.')
        _orca_protocol(text, analysis)
        # The charge/multiplicity card is explicit input evidence, useful in
        # cropped files, and is never inferred from atoms or occupations.
        charge_cards = list(re.finditer(r'^\s*(?:\|\s*\d+>\s*)?\*\s*(?:xyz|xyzfile|int)\s+([-+]?\d+)\s+(\d+)\b', text, re.I | re.M))
        if charge_cards:
            for key, index in [('charge', 1), ('multiplicity', 2)]:
                if (analysis.get('system') or {}).get(key) is None:
                    analysis.setdefault('system', {})[key] = int(charge_cards[-1].group(index))
                    analysis['system'][key + '_source'] = 'explicit input coordinate card'
    else:
        analysis['orbitals'] = _psi4_orbitals_v59(text)
        analysis['final_geometry'], analysis['geometry_units'], analysis['source_geometry_units'] = _psi4_geometry_strict(text)
        analysis['freq_layout'] = 'VIBRATION_ONLY' if analysis.get('frequencies_cm1') else None
        analysis['frequency_layout'] = analysis['freq_layout']
        analysis['frequency_modes'] = [{'mode': None, 'frequency_cm1': value} for value in analysis.get('frequencies_cm1') or []]
    analysis['optimization_energies'] = _optimization_trace(text, engine)
    analysis['dipole'] = _dipole_strict(text, engine)
    analysis['orbitals'] = _frontiers_complete((analysis.get('orbitals') or {}).get('orbitals') or [])
    analysis['conceptual_dft'] = conceptual_dft_descriptors(analysis)
    thermo, blocks = _strict_thermochemistry(text, engine)
    analysis['thermochemistry'], analysis['thermochemistry_blocks'] = thermo, blocks
    reference = thermo.get('electronic_reference_hartree')
    final = (analysis.get('energies') or {}).get('final_energy_hartree')
    analysis['thermo_matches_final_energy'] = abs(reference - final) < 1e-6 if reference is not None and final is not None else None
    if analysis['thermo_matches_final_energy'] is False:
        warnings.append('Thermochemistry belongs to an earlier electronic-energy record; a later single point was not combined with its corrections.')
    if len(blocks) > 1:
        warnings.append('Multiple thermochemistry blocks were retained separately; the last block is the primary thermochemistry result.')
    if analysis.get('optimization_converged') is False:
        warnings.append('Geometry optimization did not converge; normal program termination does not establish a stationary structure.')
    if analysis.get('scf_converged') is False:
        warnings.append('The final recognized SCF convergence record reports nonconvergence.')
    if analysis.get('final_geometry') and analysis.get('geometry_units') is None:
        warnings.append('Coordinate units could not be confirmed; no Angstrom label or conversion was inferred.')
    # ROOT's quality gate evaluates stationarity and reliability. The parser
    # provides evidence; it does not promote partial positive frequencies.
    analysis['stationary_point_status'] = 'NOT_ASSESSED'
    analysis['thermochemistry_reliability'] = 'NOT_ASSESSED'
    analysis['warnings'] = list(dict.fromkeys(warnings))
    analysis['parser_warnings'] = list(analysis['warnings'])
    for key in ('final_energy_hartree', 'final_energy_ev', 'final_energy_kj_mol'):
        analysis.setdefault('energies', {}).setdefault(key, None)
    return analysis


def parse_output_text(text, filename='output.out'):
    """Public parser: explicit evidence, complete arrays and block provenance."""
    engine = detect_engine(text)
    if engine not in ('ORCA', 'Psi4'):
        analysis = _LEGACY_ANALYZER_PARSE(text, filename)
        analysis['normal_termination'] = None
        analysis.update({'optimization_requested': None, 'optimization_converged': None, 'scf_converged': None})
        analysis['warnings'] = ['Calculation engine could not be identified; scientific interpretation is unavailable.']
        analysis['parser_warnings'] = list(analysis['warnings'])
        analysis['stationary_point_status'] = 'NOT_ASSESSED'
        analysis['thermochemistry_reliability'] = 'NOT_ASSESSED'
        return analysis
    blocks = _orca_split_jobs(text) if engine == 'ORCA' else _psi4_split_jobs(text)
    parsed = [_parse_one_scientific(block, filename, engine) for block in blocks]
    primary = parsed[-1] if parsed else _parse_one_scientific('', filename, engine)
    if len(parsed) > 1:
        primary['warnings'].append('Multiple calculation jobs are present; the final job is shown without combining prior jobs.')
        primary['parser_warnings'] = list(primary['warnings'])
    primary['job_blocks_count'] = len(parsed)
    primary['job_blocks'] = [{key: job.get(key) for key in ('engine', 'method', 'basis', 'dispersion', 'normal_termination', 'optimization_requested', 'optimization_converged', 'scf_converged', 'energies', 'thermochemistry', 'final_geometry', 'geometry_units')} for job in parsed]
    if engine == 'ORCA':
        primary['orca_job_blocks_count'] = len(parsed)
        primary['orca_job_blocks'] = [dict(index=index, **{key: job.get(key) for key in ('method', 'basis', 'normal_termination', 'optimization_converged', 'scf_converged')}, final_energy_hartree=(job.get('energies') or {}).get('final_energy_hartree'), tddft_states=len(job.get('tddft_states') or []), frequencies=len(job.get('frequencies_cm1') or [])) for index, job in enumerate(parsed, 1)]
    return primary



"""Conservative Gaussian text-output adapter, without engine/network execution.

Scope: common HF/DFT, MP2/MP3 and CCSD/CCSD(T) output, printed Cartesian
orientations, harmonic frequencies, populations, eigenvalues and transitions.
Checkpoint extraction is intentionally left to the existing worker pipeline.
Append after analyzer_core.py and before scientific validation/reporting.
"""
import math
import re

try:
    _GAUSSIAN_PREVIOUS_PARSE = parse_output_text
    _GAUSSIAN_PREVIOUS_DETECT = detect_engine
except NameError:
    _GAUSSIAN_PREVIOUS_PARSE = None
    _GAUSSIAN_PREVIOUS_DETECT = None

_GAUSS_FLOAT = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?'
_GAUSS_HARTREE_EV = 27.211386245988
_GAUSS_HARTREE_KJMOL = 2625.499638
_GAUSS_SYMBOLS = ('X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split()


def _gauss_number(value):
    try:
        result = float(str(value).replace('D', 'E').replace('d', 'e'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _gauss_last(text, pattern, group=1):
    matches = list(re.finditer(pattern, text, re.I | re.M))
    return _gauss_number(matches[-1].group(group)) if matches else None


def _is_gaussian_output(text):
    return bool(re.search(r'^\s*(?:Gaussian\s+(?:16|09)\s*:|Entering\s+Gaussian\s+System|Normal\s+termination\s+of\s+Gaussian\s+(?:16|09))', str(text or ''), re.I | re.M))


def _gauss_jobs(text):
    blocks, current, results_seen = [], [], False
    boundary = re.compile(r'^\s*(?:Link1:\s*Proceeding\s+to\s+internal\s+job\s+step|--Link1--\s*$|Entering\s+Link\s+1\s*=|Gaussian\s+(?:16|09)\s*:)', re.I)
    for line in str(text or '').splitlines():
        if boundary.match(line) and current and results_seen:
            blocks.append('\n'.join(current))
            current, results_seen = [], False
        current.append(line)
        if re.search(r'SCF\s+Done:|Normal\s+termination|Error\s+termination|Frequencies\s*--', line, re.I):
            results_seen = True
    if current:
        blocks.append('\n'.join(current))
    return [block for block in blocks if block.strip()]


def _gauss_route(text):
    routes, current, active = [], [], False
    for line in text.splitlines():
        if re.match(r'^\s*#\s*\S', line):
            if current:
                routes.append(' '.join(current))
            current, active = [line.strip()], True
        elif active:
            if not line.strip() or set(line.strip()) <= set('-='):
                routes.append(' '.join(current))
                current, active = [], False
            else:
                current.append(line.strip())
    if current:
        routes.append(' '.join(current))
    return routes[-1] if routes else None


def _gauss_requested_method(route):
    if not route:
        return None
    for pattern, method in [
        (r'(?<![\w-])CCSD\(T\)(?![\w-])', 'CCSD(T)'),
        (r'(?<![\w-])CCSD(?![\w(])', 'CCSD'),
        (r'(?<![\w-])(?:U|R)?MP3(?![\w])', 'MP3'),
        (r'(?<![\w-])(?:U|R)?MP2(?![\w])', 'MP2'),
    ]:
        if re.search(pattern, route, re.I):
            return method
    method = re.search(r'(?<!\S)([A-Za-z][A-Za-z0-9()+\-]*)\s*/', route)
    return method.group(1) if method else None


def _gauss_scf_method(raw):
    upper = raw.upper()
    if upper in ('RHF', 'UHF', 'ROHF', 'HF'):
        return 'HF', upper
    reference = 'UKS' if upper.startswith('U') else ('RKS' if upper.startswith('R') else None)
    method = raw[1:] if reference else raw
    aliases = {'PBE1PBE': 'PBE0', 'B3LYP': 'B3LYP', 'CAM-B3LYP': 'CAM-B3LYP'}
    return aliases.get(method.upper(), method), reference


def _gauss_energies(text, requested):
    candidates, scf_records = [], []
    pattern = r'SCF\s+Done:\s+E\(([^)]+)\)\s*=\s*(' + _GAUSS_FLOAT + r')'
    for match in re.finditer(pattern, text, re.I):
        value = _gauss_number(match.group(2))
        method, reference = _gauss_scf_method(match.group(1))
        if value is not None:
            record = {'offset': match.start(), 'value': value, 'method': method, 'reference': reference, 'source': 'SCF Done'}
            scf_records.append(record)
            candidates.append(record)
    for pattern, method in [
        (r'\bCCSD\(T\)\s*=\s*(' + _GAUSS_FLOAT + r')', 'CCSD(T)'),
        (r'\bE\(CCSD\)\s*=\s*(' + _GAUSS_FLOAT + r')', 'CCSD'),
        (r'\bE(?:U|R)?MP3\s*=\s*(' + _GAUSS_FLOAT + r')', 'MP3'),
        (r'\bE(?:U|R)?MP2\s*=\s*(' + _GAUSS_FLOAT + r')', 'MP2'),
        (r'\bE\((?:R|U)?MP2\)\s*=\s*(' + _GAUSS_FLOAT + r')', 'MP2'),
    ]:
        for match in re.finditer(pattern, text, re.I):
            value = _gauss_number(match.group(1))
            if value is not None:
                candidates.append({'offset': match.start(), 'value': value, 'method': method, 'reference': None, 'source': match.group(0).split('=')[0].strip()})
    if requested in ('CCSD', 'CCSD(T)'):
        # E(CORR) is a CC total only in a recognized CC route; in other
        # methods this ambiguous label is not assigned as a final energy.
        for match in re.finditer(r'\bE\(CORR\)\s*=\s*(' + _GAUSS_FLOAT + r')', text, re.I):
            value = _gauss_number(match.group(1))
            if value is not None:
                candidates.append({'offset': match.start(), 'value': value, 'method': 'CCSD', 'reference': None, 'source': 'E(CORR) in explicit CCSD route'})
    selected = max(candidates, key=lambda record: record['offset']) if candidates else None
    reference = scf_records[-1]['reference'] if scf_records else None
    energies = {'final_energy_hartree': None, 'final_energy_kj_mol': None, 'final_energy_ev': None,
                'scf_energy_hartree': scf_records[-1]['value'] if scf_records else None}
    if selected:
        energies.update(final_energy_hartree=selected['value'], final_energy_kj_mol=selected['value']*_GAUSS_HARTREE_KJMOL,
                        final_energy_ev=selected['value']*_GAUSS_HARTREE_EV, final_energy_source=selected['source'])
    return energies, selected['method'] if selected else None, reference, scf_records


def _gauss_orientation(text):
    tables, rows, active, units = [], [], False, None
    row = re.compile(r'^\s*(\d+)\s+(-?\d+)\s+(-?\d+)\s+(' + _GAUSS_FLOAT + r')\s+(' + _GAUSS_FLOAT + r')\s+(' + _GAUSS_FLOAT + r')\s*$')
    for line in text.splitlines():
        if re.match(r'^\s*(?:Standard|Input|Z-Matrix)\s+orientation\s*:', line, re.I):
            if rows:
                tables.append((rows, units))
            rows, active, units = [], True, None
            continue
        if not active:
            continue
        if re.search(r'Coordinates\s*\(Angstroms?\)', line, re.I):
            units = 'angstrom'
        match = row.match(line)
        if match:
            atomic = int(match.group(2))
            xyz = [_gauss_number(match.group(index)) for index in (4, 5, 6)]
            if not all(value is not None for value in xyz):
                continue
            symbol = _GAUSS_SYMBOLS[atomic] if 0 < atomic < len(_GAUSS_SYMBOLS) else 'X'
            rows.append({'element': symbol, 'x': xyz[0], 'y': xyz[1], 'z': xyz[2], 'source_index': int(match.group(1)), 'atomic_number': atomic})
        elif rows:
            tables.append((rows, units))
            rows, active = [], False
    if rows:
        tables.append((rows, units))
    return tables[-1] if tables else ([], None)


def _gauss_vibrations(text):
    blocks, rows, group, indices, active = [], [], [], [], False
    for offset, line in enumerate(text.splitlines()):
        if re.search(r'^\s*Harmonic\s+frequencies\s*\(', line, re.I):
            if rows:
                blocks.append(rows)
            rows, group, indices, active = [], [], [], True
            continue
        if re.match(r'^\s*(?:-+\s*)?Thermochemistry\s*(?:-+)?\s*$', line, re.I):
            active = False
            continue
        match = re.match(r'^\s*Frequencies\s*--\s*(.*)$', line, re.I)
        if match:
            active = True
            values = [_gauss_number(token) for token in match.group(1).split()]
            group = [{'mode': indices[index] if index < len(indices) else None,
                      'frequency_cm1': value, 'intensity_km_mol': None,
                      'activity_angstrom4_amu': None, 'depolarization_ratio': None}
                     for index, value in enumerate(values)]
            rows.extend(record for record in group if record['frequency_cm1'] is not None)
            indices = []
            continue
        if not active:
            continue
        if re.fullmatch(r'\s*\d+(?:\s+\d+){0,2}\s*', line):
            indices = [int(token) for token in line.split()]
        for label, key in [(r'IR\s+Inten', 'intensity_km_mol'), (r'Raman\s+Activ', 'activity_angstrom4_amu'), (r'Depolar\s*\(P\)', 'depolarization_ratio')]:
            match = re.match(r'^\s*'+label+r'\s*--\s*(.*)$', line, re.I)
            if match and group:
                values = [_gauss_number(token) for token in match.group(1).split()]
                for record, value in zip(group, values):
                    record[key] = value
    if rows:
        blocks.append(rows)
    return blocks[-1] if blocks else [], blocks


def _gauss_orbitals(text, reference):
    rows, tables = [], []
    seen_virtual = False
    pattern = re.compile(r'^\s*(Alpha|Beta)\s+(occ\.|virt\.)\s+eigenvalues\s*--\s*(.*)$', re.I)
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        spin, occupancy = match.group(1).lower(), 1.0 if match.group(2).lower() == 'occ.' else 0.0
        if spin == 'alpha' and occupancy == 1.0 and seen_virtual:
            if rows:
                tables.append(rows)
            rows, seen_virtual = [], False
        if spin == 'alpha' and occupancy == 0.0:
            seen_virtual = True
        for token in match.group(3).split():
            energy = _gauss_number(token)
            if energy is not None:
                label_spin = 'restricted' if reference in ('RHF', 'RKS') else spin
                occ = 2.0 if occupancy == 1.0 and label_spin == 'restricted' else occupancy
                index = sum(record['spin'] == label_spin for record in rows) + 1
                rows.append({'index': index, 'spin': label_spin, 'occ': occ, 'eh': energy, 'ev': energy*_GAUSS_HARTREE_EV})
    if rows:
        tables.append(rows)
    records = tables[-1] if tables else []
    if '_frontiers_complete' in globals():
        return _frontiers_complete(records)
    occupied = [record['ev'] for record in records if record['occ'] > 0]
    virtual = [record['ev'] for record in records if record['occ'] == 0]
    h, l = max(occupied, default=None), min(virtual, default=None)
    return {'orbitals': records, 'homo_ev': h, 'lumo_ev': l, 'gap_ev': l-h if h is not None and l is not None else None}


def _gauss_thermo(text, scf_records, method, basis):
    header = list(re.finditer(r'^\s*(?:-+\s*)?Thermochemistry\s*(?:-+)?\s*$|^\s*Temperature\s+' + _GAUSS_FLOAT + r'\s+Kelvin', text, re.I | re.M))
    # Temperature directly following a Thermochemistry header is in the same
    # block. Start a new block only after previously printed thermal totals.
    starts = []
    for match in header:
        if not starts or re.search(r'Sum of electronic and', text[starts[-1]:match.start()], re.I):
            starts.append(match.start())
    if not starts and re.search(r'Zero-point correction|Sum of electronic and thermal', text, re.I):
        starts = [0]
    blocks, warnings = [], []
    for index, start in enumerate(starts):
        segment = text[start:(starts[index+1] if index+1 < len(starts) else len(text))]
        t = {key: None for key in ('temperature_K', 'pressure_atm', 'zpe_hartree', 'energy_0K_hartree', 'thermal_energy_correction_hartree', 'enthalpy_correction_hartree', 'gibbs_correction_hartree', 'internal_energy_hartree', 'thermal_energy_hartree', 'enthalpy_hartree', 'gibbs_hartree', 'entropy_J_mol_K', 'electronic_energy_hartree', 'electronic_reference_hartree', 'thermo_treatment', 'thermo_method', 'thermo_basis')}
        t['temperature_K'] = _gauss_last(segment, r'\bTemperature\s+('+_GAUSS_FLOAT+r')\s+Kelvin')
        t['pressure_atm'] = _gauss_last(segment, r'\bPressure\s+('+_GAUSS_FLOAT+r')\s+Atm\b')
        labels = {
            'zpe_hartree': r'Zero-point\s+correction',
            'thermal_energy_correction_hartree': r'Thermal\s+correction\s+to\s+Energy',
            'enthalpy_correction_hartree': r'Thermal\s+correction\s+to\s+Enthalpy',
            'gibbs_correction_hartree': r'Thermal\s+correction\s+to\s+Gibbs\s+Free\s+Energy',
            'energy_0K_hartree': r'Sum\s+of\s+electronic\s+and\s+zero-point\s+Energies',
            'internal_energy_hartree': r'Sum\s+of\s+electronic\s+and\s+thermal\s+Energies',
            'enthalpy_hartree': r'Sum\s+of\s+electronic\s+and\s+thermal\s+Enthalpies',
            'gibbs_hartree': r'Sum\s+of\s+electronic\s+and\s+thermal\s+Free\s+Energies',
        }
        for key, label in labels.items():
            t[key] = _gauss_last(segment, label+r'\s*=\s*('+_GAUSS_FLOAT+r')')
        references = []
        for total, correction in [('energy_0K_hartree', 'zpe_hartree'), ('internal_energy_hartree', 'thermal_energy_correction_hartree'), ('enthalpy_hartree', 'enthalpy_correction_hartree'), ('gibbs_hartree', 'gibbs_correction_hartree')]:
            if t[total] is not None and t[correction] is not None:
                references.append(t[total]-t[correction])
        previous = [record for record in scf_records if record['offset'] < start]
        if references and max(references)-min(references) <= 5e-6:
            t['electronic_energy_hartree'] = references[-1]
            t['reference_energy_source'] = 'printed thermal sum minus its printed correction'
        elif references:
            warnings.append('Printed Gaussian thermal sums/corrections give inconsistent electronic reference energies.')
        elif previous:
            t['electronic_energy_hartree'] = previous[-1]['value']
            t['reference_energy_source'] = 'preceding SCF Done record; no paired thermal sum/correction available'
        t['electronic_reference_hartree'] = t['electronic_energy_hartree']
        if previous and t['electronic_energy_hartree'] is not None and abs(t['electronic_energy_hartree']-previous[-1]['value']) <= 5e-6:
            t['thermo_method'] = previous[-1]['method']
            t['thermo_basis'] = basis
        # Explicit harmonic frequency context identifies the approximation;
        # the default is not inferred merely from a Gaussian version.
        if re.search(r'Harmonic\s+frequencies\s*\(', text[:start], re.I):
            t['thermo_treatment'] = 'Gaussian harmonic thermochemistry'
        t['thermal_energy_hartree'] = t['internal_energy_hartree']
        h, g, temp = t['enthalpy_hartree'], t['gibbs_hartree'], t['temperature_K']
        if h is not None and g is not None and temp is not None and temp > 0:
            t['entropy_J_mol_K'] = (h-g)*_GAUSS_HARTREE_KJMOL*1000/temp
            t['entropy_source'] = '(H-G)/T from printed totals'
        t['block_index'] = index+1
        for key in list(t):
            if key.endswith('_hartree') and t[key] is not None:
                t[key.replace('_hartree', '_kj_mol')] = t[key]*_GAUSS_HARTREE_KJMOL
        if any(t[key] is not None for key in ('zpe_hartree', 'enthalpy_hartree', 'gibbs_hartree')):
            blocks.append(t)
    empty = {key: None for key in ('temperature_K', 'pressure_atm', 'zpe_hartree', 'enthalpy_hartree', 'gibbs_hartree', 'electronic_energy_hartree', 'electronic_reference_hartree', 'thermo_method', 'thermo_basis', 'thermo_treatment')}
    return dict(blocks[-1]) if blocks else empty, blocks, warnings


def _parse_gaussian_one(text, filename):
    warnings = []
    route = _gauss_route(text)
    requested = _gauss_requested_method(route)
    energies, method, reference, scf_records = _gauss_energies(text, requested)
    standard = list(re.finditer(r'^\s*Standard\s+basis:\s*([^\n]+)', text, re.I | re.M))
    basis = standard[-1].group(1).strip().split()[0] if standard else None
    basis_details = standard[-1].group(1).strip() if standard else None
    if basis is None and route:
        basis_match = re.search(r'/\s*([^\s]+)', route)
        basis = basis_match.group(1) if basis_match else None
    normal_marks = [(match.start(), True) for match in re.finditer(r'^\s*Normal\s+termination\s+of\s+Gaussian\b', text, re.I | re.M)]
    fail_pattern = r'^\s*Error\s+termination|Convergence\s+failure\s*--|Erroneous\s+write|Out\s+of\s+memory|galloc:|Segmentation\s+violation'
    normal_marks += [(match.start(), False) for match in re.finditer(fail_pattern, text, re.I | re.M)]
    normal = max(normal_marks, key=lambda pair: pair[0])[1] if normal_marks else None
    scf_failed = list(re.finditer(r'Convergence\s+failure\s*--|SCF\s+(?:failed|did\s+not\s+converge)', text, re.I))
    last_scf = scf_records[-1]['offset'] if scf_records else -1
    scf_converged = False if scf_failed and scf_failed[-1].start() > last_scf else (True if scf_records else None)
    opt_marks = [(match.start(), True) for match in re.finditer(r'^\s*(?:Optimization\s+completed\.|Stationary\s+point\s+found\.)', text, re.I | re.M)]
    opt_marks += [(match.start(), False) for match in re.finditer(r'^\s*Optimization\s+stopped\.|Number\s+of\s+steps\s+exceeded', text, re.I | re.M)]
    opt_converged = max(opt_marks, key=lambda pair: pair[0])[1] if opt_marks else None
    opt_requested = True if opt_marks or re.search(r'Berny\s+optimization|\bOpt(?:\b|=|\()', route or '', re.I) else (False if route else None)
    geometry, geometry_units = _gauss_orientation(text)
    vibrations, vibration_blocks = _gauss_vibrations(text)
    thermo, thermo_blocks, thermo_warnings = _gauss_thermo(text, scf_records, method, basis)
    warnings.extend(thermo_warnings)
    if len(vibration_blocks) > 1:
        warnings.append('Multiple Gaussian harmonic frequency tables are retained separately; the last table is used.')
    if len(thermo_blocks) > 1:
        warnings.append('Multiple Gaussian thermochemistry blocks are retained separately; the last block is used.')
    if geometry and geometry_units is None:
        warnings.append('Gaussian Cartesian orientation units were not confirmed.')
    if requested in ('MP2', 'MP3', 'CCSD', 'CCSD(T)') and method != requested:
        warnings.append('Requested correlated method has no matching recognized final total energy; the parsed energy is labeled by its printed method.')
        energies.update(final_energy_hartree=None, final_energy_kj_mol=None, final_energy_ev=None)
        method = None
    unsupported = bool(re.search(r'\b(?:ONIOM|CBS-QB3|G[234]|W1BD|W1U|W2U|QCISD|CAS|CASSCF)\b', route or '', re.I))
    if unsupported:
        warnings.append('This composite/multireference/ONIOM route is outside the supported Gaussian energy parser; subsystem SCF energies are not labeled as a final total.')
        energies.update(final_energy_hartree=None, final_energy_kj_mol=None, final_energy_ev=None)
        method = None
    numerical_frequency = bool(re.search(r'\bFreq\s*=\s*\(?\s*(?:[^\n)]*\b)?Numer(?:ical)?\b', route or '', re.I))
    if numerical_frequency:
        warnings.append('Numerical-frequency SCF displacement energies are not promoted to a final equilibrium electronic energy.')
        energies.update(final_energy_hartree=None, final_energy_kj_mol=None, final_energy_ev=None)
    system_match = list(re.finditer(r'\bCharge\s*=\s*([-+]?\d+)\s+Multiplicity\s*=\s*(\d+)', text, re.I))
    system = {'charge': int(system_match[-1].group(1)) if system_match else None, 'multiplicity': int(system_match[-1].group(2)) if system_match else None}
    dipole = {}
    dipoles = list(re.finditer(r'Dipole\s+moment\s*\([^\n]*Debye\)\s*:\s*\n\s*X\s*=\s*('+_GAUSS_FLOAT+r')\s+Y\s*=\s*('+_GAUSS_FLOAT+r')\s+Z\s*=\s*('+_GAUSS_FLOAT+r')\s+Tot\s*=\s*('+_GAUSS_FLOAT+r')', text, re.I))
    if dipoles:
        values = [_gauss_number(dipoles[-1].group(index)) for index in range(1,5)]
        dipole = {'vector': values[:3], 'magnitude_debye': values[3], 'units': 'debye'}
    transitions = []
    transition_pattern = r'Excited\s+State\s+(\d+)\s*:\s*(.*?)\s+('+_GAUSS_FLOAT+r')\s+eV\s+('+_GAUSS_FLOAT+r')\s+nm\s+f\s*=\s*('+_GAUSS_FLOAT+r')'
    for match in re.finditer(transition_pattern, text, re.I):
        ev, nm, strength = [_gauss_number(match.group(index)) for index in (3,4,5)]
        state_index = int(match.group(1))
        if transitions and state_index <= transitions[-1]['state']:
            transitions = []
        if ev is not None and ev > 0 and nm is not None and nm > 0 and strength is not None and strength >= 0:
            if abs(ev * nm - 1239.841984) > 2.5:
                warnings.append('An inconsistent Gaussian excitation energy/wavelength pair was excluded from spectral rendering.')
                continue
            transitions.append({'state': state_index, 'symmetry': match.group(2).strip(), 'ev': ev, 'nm': nm, 'f': strength, 'cm1': ev*8065.544005})
    charges, active = [], False
    for line in text.splitlines():
        if re.match(r'^\s*Mulliken\s+charges(?:\s+and\s+spin\s+densities)?\s*:', line, re.I):
            charges, active = [], True
            continue
        if active:
            match = re.match(r'^\s*(\d+)\s+([A-Za-z]{1,2})\s+('+_GAUSS_FLOAT+r')(?:\s|$)', line)
            if match:
                charges.append({'index': int(match.group(1)), 'element': match.group(2), 'charge': _gauss_number(match.group(3))})
            elif charges:
                active = False
    orbitals = _gauss_orbitals(text, reference)
    dispersion = None
    dispersion_match = re.search(r'EmpiricalDispersion\s*=\s*(?:\()?\s*(GD3BJ|GD3|GD2)', route or '', re.I)
    if dispersion_match:
        dispersion = {'GD3BJ': 'D3BJ', 'GD3': 'D3', 'GD2': 'D2'}[dispersion_match.group(1).upper()]
    solvent_match = re.search(r'Solvent\s*=\s*([A-Za-z0-9()_\-]+)', route or '', re.I)
    solvent = solvent_match.group(1).rstrip(')') if solvent_match else None
    solvation = 'SMD' if re.search(r'\bSCRF\s*=.*?\bSMD\b', route or '', re.I) else ('PCM' if re.search(r'\bSCRF\b', route or '', re.I) else None)
    frequencies = [record['frequency_cm1'] for record in vibrations]
    final = energies.get('final_energy_hartree')
    thermal_ref = thermo.get('electronic_energy_hartree')
    compatible = abs(final-thermal_ref) <= 5e-6 if final is not None and thermal_ref is not None else None
    if compatible is False:
        warnings.append('Gaussian thermochemistry reference differs from the final electronic energy; no composite thermal correction was applied.')
    version = re.search(r'Gaussian\s+(16|09)\s*:', text, re.I)
    revision = re.search(r'G(?:16|09)Rev([A-Za-z0-9.]+)', text, re.I)
    analysis = {
        'filename': filename, 'engine': 'Gaussian', 'version': (version.group(1) if version else None), 'revision': revision.group(1) if revision else None,
        'parser_version': 'Gaussian-common-text-1.0', 'gaussian_parser_scope': 'Common HF/DFT, MP2/MP3 and CCSD/CCSD(T) text output; other protocols remain unverified',
        'normal_termination': normal, 'errors': [line.strip() for line in text.splitlines() if re.search(fail_pattern, line, re.I)],
        'optimization_requested': opt_requested, 'optimization_converged': opt_converged, 'scf_converged': scf_converged,
        'method': method, 'method_requested': requested, 'basis': basis, 'basis_details': basis_details, 'reference': reference, 'dispersion': dispersion,
        'route': route, 'solvation_model': solvation, 'solvent': solvent, 'method_components': {},
        'energies': energies, 'thermochemistry': thermo, 'thermochemistry_blocks': thermo_blocks, 'thermo_matches_final_energy': compatible,
        'final_geometry': geometry, 'geometry_units': geometry_units, 'source_geometry_units': geometry_units, 'system': system,
        'frequency_modes': vibrations, 'frequency_blocks': vibration_blocks, 'frequencies_cm1': frequencies,
        'imaginary_frequencies_cm1': [value for value in frequencies if value < 0], 'frequency_layout': 'VIBRATION_ONLY' if vibrations else None,
        'ir_spectrum': [(record['frequency_cm1'], record['intensity_km_mol']) for record in vibrations if record['intensity_km_mol'] is not None],
        'raman_spectrum': [(record['frequency_cm1'], record['activity_angstrom4_amu']) for record in vibrations if record['activity_angstrom4_amu'] is not None],
        'ir_modes': vibrations, 'raman_modes': vibrations, 'raman_quantity': 'scattering activity', 'raman_units': 'angstrom^4/amu',
        'orbitals': orbitals, 'dipole': dipole, 'atomic_charges': charges, 'atomic_charge_type': 'mulliken' if charges else None,
        'tddft_states': transitions, 'timings': {'wall_seconds': None, 'cpu_seconds': None}, 'optimization_energies': [],
        'stationary_point_status': 'NOT_ASSESSED', 'thermochemistry_reliability': 'NOT_ASSESSED', 'warnings': warnings, 'parser_warnings': list(warnings),
    }
    if 'conceptual_dft_descriptors' in globals():
        analysis['conceptual_dft'] = conceptual_dft_descriptors(analysis)
    else:
        analysis['conceptual_dft'] = {}
    for label, key in [('Elapsed time', 'wall_seconds'), ('Job cpu time', 'cpu_seconds')]:
        match = re.search(label+r':\s*(\d+)\s+days\s+(\d+)\s+hours\s+(\d+)\s+minutes\s+('+_GAUSS_FLOAT+r')\s+seconds', text, re.I)
        if match:
            days, hours, minutes, seconds = [_gauss_number(value) for value in match.groups()]
            analysis['timings'][key] = days*86400+hours*3600+minutes*60+seconds
    return analysis


def detect_engine(text):
    if _is_gaussian_output(text):
        return 'Gaussian'
    return _GAUSSIAN_PREVIOUS_DETECT(text) if _GAUSSIAN_PREVIOUS_DETECT else 'Unknown'


def parse_output_text(text, filename='output.out'):
    if not _is_gaussian_output(text):
        if _GAUSSIAN_PREVIOUS_PARSE:
            return _GAUSSIAN_PREVIOUS_PARSE(text, filename)
        return {'filename': filename, 'engine': 'Unknown', 'normal_termination': None,
                'warnings': ['Gaussian output identity could not be confirmed.']}
    blocks = [_parse_gaussian_one(block, filename) for block in _gauss_jobs(text)]
    primary = blocks[-1]
    primary['job_blocks_count'] = len(blocks)
    primary['job_blocks'] = [{key: block.get(key) for key in ('method', 'basis', 'normal_termination', 'energies', 'thermochemistry', 'final_geometry', 'optimization_converged', 'scf_converged')} for block in blocks]
    if len(blocks) > 1:
        primary['warnings'].append('Multiple Gaussian Link1 jobs are present; final-job properties are shown without inheriting earlier thermal corrections.')
        primary['parser_warnings'] = list(primary['warnings'])
    return primary


"""Conservative scientific validation and reaction checks (no bot/network side effects)."""
import copy
import hashlib
import math
import re
from pathlib import Path

ANALYSIS_SCHEMA_VERSION = "2.0"
PARSER_VERSION = "7.1.2"
HARTREE_TO_KJMOL = 2625.499638
HARTREE_TO_EV = 27.211386245988
GAS_CONSTANT_J_MOL_K = 8.314462618
BOHR_TO_ANGSTROM = 0.529177210903
IMAGINARY_NOISE_THRESHOLD_CM1 = 10.0


def _finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _unique_messages(messages):
    return list(dict.fromkeys(str(message) for message in messages if message))


def _clean_nonfinite(value, path="", warnings=None):
    warnings = [] if warnings is None else warnings
    if isinstance(value, float) and not math.isfinite(value):
        warnings.append("Non-finite value omitted at " + (path or "root") + ".")
        return None
    if isinstance(value, dict):
        return {str(key): _clean_nonfinite(item, path + "." + str(key), warnings)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_nonfinite(item, path + "[" + str(index) + "]", warnings)
                for index, item in enumerate(value)]
    return value


def _molecule_mode_counts(geometry):
    """Return atom count, linearity and internal-mode count; reject ghost-only data."""
    atoms = []
    for row in geometry or []:
        symbol = str(row.get("element", ""))
        xyz = [_finite_number(row.get(axis)) for axis in ("x", "y", "z")]
        if symbol.endswith(":") or symbol.upper() in ("X", "GH", "DA") or None in xyz:
            continue
        if not re.fullmatch(r"[A-Z][a-z]?", symbol):
            continue
        atoms.append(xyz)
    count = len(atoms)
    if not count:
        return None, None, None
    if count == 1:
        return 1, None, 0
    if count == 2:
        return 2, True, 1
    try:
        import numpy as np
        coordinates = np.asarray(atoms, dtype=float)
        singular = np.linalg.svd(coordinates - coordinates.mean(axis=0), compute_uv=False)
        if not len(singular) or singular[0] < 1e-8:
            return count, None, None
        linear = bool(len(singular) < 2 or singular[1] <= max(1e-6, singular[0] * 1e-6))
    except ImportError:
        return count, None, None
    return count, linear, 3 * count - (5 if linear else 6)


def _theory_signature(analysis):
    details = analysis.get("method_details") or {}
    reference = analysis.get("reference") or (analysis.get("psi4_details") or {}).get("reference")
    return {
        "method": analysis.get("method"),
        "basis": analysis.get("basis"),
        "dispersion": analysis.get("dispersion"),
        "reference": reference,
        "relativistic": analysis.get("relativistic") or details.get("relativistic"),
        "solvation_model": analysis.get("solvation_model"),
        "solvent": analysis.get("solvent"),
        "basis_details": analysis.get("basis_details"),
    }


def validate_analysis(analysis, source_text=None):
    """Validate available evidence; missing checks never become convergence claims."""
    if not isinstance(analysis, dict):
        raise TypeError("Analysis must be a dictionary.")
    warnings = list(analysis.get("warnings") or []) + list(analysis.get("parser_warnings") or [])
    a = _clean_nonfinite(copy.deepcopy(analysis), warnings=warnings)
    for key in ("energies", "thermochemistry", "system", "orbitals", "provenance"):
        if not isinstance(a.get(key), dict):
            a[key] = {}
    a["geometry_units"] = a.get("geometry_units") or "UNKNOWN"
    a["analysis_schema_version"] = ANALYSIS_SCHEMA_VERSION
    a["parser_version"] = PARSER_VERSION
    if source_text is not None:
        a["source_sha256"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        a["source_hash_scope"] = "UTF-8 decoded output text"
    a["units"] = {
        "energy": "hartree", "orbital_energy": "eV", "geometry": a["geometry_units"],
        "frequency": "cm^-1", "ir_stick_intensity": "km mol^-1",
        "raman_activity": "angstrom^4 u^-1", "dipole": "debye",
        "temperature": "K", "pressure": "atm", "entropy": "J mol^-1 K^-1",
    }
    e = _finite_number(a["energies"].get("final_energy_hartree"))
    if e is not None:
        a["energies"]["final_energy_kj_mol"] = e * HARTREE_TO_KJMOL
        a["energies"]["final_energy_ev"] = e * HARTREE_TO_EV
    if e is not None and (not a.get("method") or not a.get("basis")):
        warnings.append("Method/basis metadata was not verified for the selected energy.")

    geom = a.get("final_geometry") or []
    count, linear, expected = _molecule_mode_counts(geom)
    if geom and a["geometry_units"] != "angstrom":
        linear, expected = None, None
        warnings.append("Coordinate units are not verified; geometry-based mode completeness is withheld.")
    frequencies = [_finite_number(value) for value in a.get("frequencies_cm1") or []]
    frequencies = [value for value in frequencies if value is not None]
    a["frequencies_cm1"] = frequencies
    layout = a.get("frequency_layout")
    rigid = []
    internal = frequencies[:]
    if count and a.get("engine") == "ORCA" and len(frequencies) == 3 * count:
        rigid_count = 3 * count - expected if expected is not None else None
        if rigid_count is not None and all(abs(v) <= IMAGINARY_NOISE_THRESHOLD_CM1 for v in frequencies[:rigid_count]):
            rigid, internal = frequencies[:rigid_count], frequencies[rigid_count:]
            layout = "3N_WITH_RIGID"
        else:
            warnings.append("Rigid-body modes could not be safely separated from the 3N frequency table.")
    elif expected is not None and len(frequencies) == expected:
        layout = "VIBRATION_ONLY"
    frequency_complete = (len(internal) == expected) if expected is not None and (frequencies or expected == 0) else None
    if expected is not None and frequencies and frequency_complete is False:
        warnings.append("Frequency table is incomplete or its mode layout is not verified.")
    a["frequency_layout"] = layout or "UNKNOWN"
    a["internal_frequencies_cm1"] = internal
    a["rigid_body_frequencies_cm1"] = rigid
    a["expected_vibration_count"] = expected
    a["imaginary_frequencies_cm1"] = [v for v in internal if v < -IMAGINARY_NOISE_THRESHOLD_CM1]
    a["near_zero_imaginary_frequencies_cm1"] = [v for v in internal if -IMAGINARY_NOISE_THRESHOLD_CM1 <= v < 0]
    significant = a["imaginary_frequencies_cm1"]
    weak = a["near_zero_imaginary_frequencies_cm1"]
    if weak:
        warnings.append("Small negative internal frequencies require mode inspection; they are not automatically accepted as minima.")
    if significant:
        warnings.append("Imaginary internal modes require displacement/gradient inspection; a transition state also needs reaction-path verification.")

    termination = a.get("normal_termination")
    normal = termination is True
    scf = a.get("scf_converged") if a.get("scf_converged") in (True, False, None) else None
    opt_requested = a.get("optimization_requested")
    opt = a.get("optimization_converged") if a.get("optimization_converged") in (True, False, None) else None
    stationary_verified = opt is True or a.get("gradient_converged") is True or count == 1
    process_rc = a.get("process_returncode")
    process_failed = process_rc is not None and process_rc != 0
    failed = termination is False or process_failed or scf is False or (opt_requested is True and opt is False)
    if not normal:
        warnings.append("Normal program termination was not confirmed.")
    if process_failed:
        warnings.append("Calculation process returned a non-zero exit status.")
    if scf is False:
        warnings.append("SCF convergence failed.")
    elif scf is None:
        warnings.append("SCF convergence was not confirmed from a recognized runtime record.")
    if opt_requested is True and opt is not True:
        warnings.append("Requested geometry optimization was not confirmed converged.")
    if frequencies and not stationary_verified:
        warnings.append("Stationarity of the frequency geometry was not independently confirmed.")
    if failed:
        point = "FAILED_OR_UNCONVERGED"
    elif not normal:
        point = "UNCONFIRMED_CALCULATION"
    elif scf is not True:
        point = "UNVERIFIED_ELECTRONIC_STATE"
    elif frequency_complete is not True:
        point = "NO_VERIFIED_HESSIAN" if not frequencies else "INCOMPLETE_FREQUENCIES"
    elif len(significant) > 1:
        point = "HIGHER_ORDER_SADDLE_CANDIDATE"
    elif len(significant) == 1:
        point = "FIRST_ORDER_SADDLE_CANDIDATE"
    elif weak:
        point = "NEAR_ZERO_IMAGINARY_MODES"
    elif stationary_verified:
        point = "LIKELY_MINIMUM" if count != 1 else "MONATOMIC_SPECIES"
    else:
        point = "STATIONARITY_UNVERIFIED"

    t = a["thermochemistry"]
    temperature = _finite_number(t.get("temperature_K"))
    pressure = _finite_number(t.get("pressure_atm"))
    h = _finite_number(t.get("enthalpy_hartree"))
    g = _finite_number(t.get("gibbs_hartree"))
    thermo_e = _finite_number(t.get("electronic_energy_hartree"))
    energy_compatible = (abs(e - thermo_e) <= 5e-6) if e is not None and thermo_e is not None else None
    if energy_compatible is False:
        warnings.append("Thermochemical reference electronic energy differs from the selected final energy; no composite correction was applied.")
    elif energy_compatible is None and (h is not None or g is not None):
        warnings.append("Association of thermochemistry with the selected electronic energy was not verified.")
    entropy = _finite_number(t.get("entropy_J_mol_K"))
    if h is not None and g is not None and temperature and temperature > 0:
        derived_entropy = (h - g) * HARTREE_TO_KJMOL * 1000.0 / temperature
        t["entropy_from_H_minus_G_J_mol_K"] = derived_entropy
        if entropy is not None and abs(derived_entropy - entropy) > max(1.0, 0.02 * abs(entropy)):
            energy_compatible = False
            warnings.append("Printed entropy and (H-G)/T are inconsistent; check units and thermochemistry blocks.")
        elif entropy is None:
            t["entropy_J_mol_K"] = derived_entropy
            t["entropy_source"] = "(H-G)/T"
    if a.get("solvation_model") or a.get("solvent"):
        reference_state = t.get("standard_state") or "CONTINUUM_ELECTRONIC_PLUS_GAS_THERMAL_REFERENCE"
    else:
        reference_state = t.get("standard_state") or ("IDEAL_GAS_AT_PARSED_PRESSURE" if pressure and pressure > 0 else "UNVERIFIED")
    thermo_reliable = bool(
        normal and not failed and e is not None and scf is True and stationary_verified and frequency_complete is True
        and not significant and not weak and energy_compatible is True
        and temperature is not None and temperature > 0 and pressure is not None and pressure > 0
        and h is not None and g is not None
    )
    a["stationary_point_status"] = point
    a["thermochemistry_reliability"] = "VERIFIED_AVAILABLE_CHECKS" if thermo_reliable else "NOT_FULLY_VERIFIED"
    has_thermal_values = any(_finite_number(t.get(key)) is not None for key in
                             ("temperature_K", "zpe_hartree", "enthalpy_hartree", "gibbs_hartree", "entropy_J_mol_K"))
    if has_thermal_values and not thermo_reliable:
        warnings.append("Thermochemical values are extracted values; not all reliability checks are satisfied.")
    if e is None:
        warnings.append("A final electronic energy was not recognized.")
    status = "FAIL" if failed else ("WARNING" if warnings else ("PASS" if scf is True else "UNVERIFIED"))
    q = {
        "status": status, "program_terminated_normally": normal,
        "scf_converged": scf, "optimization_requested": opt_requested,
        "optimization_converged": opt, "frequency_complete": frequency_complete,
        "stationary_point_status": point, "thermochemistry_reliable": thermo_reliable,
        "thermochemical_energy_compatible": energy_compatible,
        "geometry_stationarity_verified": stationary_verified,
        "imaginary_noise_threshold_cm1": IMAGINARY_NOISE_THRESHOLD_CM1,
        "vibration_count": len(internal), "expected_vibration_count": expected,
        "atom_count": count, "linear_molecule": linear,
        "level_of_theory": _theory_signature(a), "standard_state": reference_state,
        "temperature_K": temperature, "pressure_atm": pressure,
        "warnings": _unique_messages(warnings),
    }
    a["quality"] = q
    a["warnings"] = q["warnings"]
    # Frontier-orbital descriptors are approximations, even for a converged SCF.
    # Withhold them for failed/unverified wavefunctions; keep raw orbital rows.
    if normal and not failed and scf is True:
        a['conceptual_dft'] = conceptual_dft_descriptors(a)
        a['conceptual_dft_validity'] = 'FRONTIER_ORBITAL_APPROXIMATION'
        if a['conceptual_dft']:
            a['conceptual_dft_convention'] = 'eta=(I-A)/2; softness=1/(I-A)=1/(2*eta)'
    else:
        a['conceptual_dft'] = {}
        a['conceptual_dft_validity'] = 'WITHHELD_UNVERIFIED_WAVEFUNCTION'
    a["provenance"].update({
        "parser_version": PARSER_VERSION, "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "source_sha256": a.get("source_sha256"), "engine": a.get("engine"),
        "engine_version": a.get("version"),
    })
    return a


def evaluate_reaction_terms(reactants, products, analyses_by_name, equation):
    """Calculate only differences supported by balanced, comparable evidence."""
    def resolve(name):
        key = Path(name).stem.lower()
        if key not in analyses_by_name:
            raise ValueError("No parsed output found for " + repr(name) + ". Upload its output first.")
        return analyses_by_name[key]
    species = [resolve(name) for _, name in reactants + products]
    if any(not a.get("normal_termination") for a in species):
        raise ValueError("Each species must confirm normal program termination.")
    warnings = []
    def canonical(value):
        if isinstance(value, dict):
            return tuple(sorted((key, canonical(item)) for key, item in value.items()))
        if isinstance(value, (tuple, list)):
            return tuple(canonical(item) for item in value)
        return str(value).strip().casefold() if value is not None else None
    signatures = [_theory_signature(a) for a in species]
    comparable = all(signature.get("method") and signature.get("basis") for signature in signatures)
    if not comparable:
        warnings.append("Method/basis was not identified for every species.")
    if len({canonical(signature) for signature in signatures}) != 1:
        comparable = False
        warnings.append("Methods, basis details, dispersion, reference, or solvation conditions differ.")
    balance = {}
    charge_delta = 0.0
    balance_known = True
    for sign, terms in ((-1.0, reactants), (1.0, products)):
        for coefficient, name in terms:
            if _finite_number(coefficient) is None or coefficient <= 0:
                raise ValueError("Stoichiometric coefficients must be positive finite numbers.")
            analysis = resolve(name)
            geometry = analysis.get("final_geometry") or []
            if not geometry:
                balance_known = False
            for atom in geometry:
                element = str(atom.get("element", ""))
                if element.endswith(":") or element.upper() in ("X", "GH", "DA"):
                    continue
                if not re.fullmatch(r"[A-Z][a-z]?", element):
                    balance_known = False
                    continue
                balance[element] = balance.get(element, 0.0) + sign * coefficient
            charge = _finite_number((analysis.get("system") or {}).get("charge"))
            if charge is None:
                balance_known = False
            else:
                charge_delta += sign * coefficient * charge
    if any(abs(value) > 1e-6 for value in balance.values()):
        raise ValueError("Reaction is not atom balanced: " + str(balance))
    if abs(charge_delta) > 1e-6:
        raise ValueError("Reaction is not charge balanced.")
    if not balance_known:
        comparable = False
        warnings.append("Atom and charge balance could not both be verified.")
    if any((a.get("quality") or {}).get("scf_converged") is not True
           or (a.get("quality") or {}).get("status") == "FAIL" for a in species):
        comparable = False
        warnings.append("Electronic convergence or calculation quality is not verified for every species.")

    def delta(getter):
        total = 0.0
        for sign, terms in ((-1.0, reactants), (1.0, products)):
            for coefficient, name in terms:
                value = _finite_number(getter(resolve(name)))
                if value is None:
                    return None
                total += sign * coefficient * value
        return total if math.isfinite(total) else None
    e = delta(lambda a: (a.get("energies") or {}).get("final_energy_hartree")) if comparable else None
    def ezpe(a):
        energy = _finite_number((a.get("energies") or {}).get("final_energy_hartree"))
        zpe = _finite_number((a.get("thermochemistry") or {}).get("zpe_hartree"))
        return energy + zpe if energy is not None and zpe is not None else None

    temperatures = [_finite_number((a.get("thermochemistry") or {}).get("temperature_K")) for a in species]
    pressures = [_finite_number((a.get("thermochemistry") or {}).get("pressure_atm")) for a in species]
    temperature = temperatures[0] if temperatures and all(t is not None and t > 0 for t in temperatures) and max(temperatures) - min(temperatures) < 1e-4 else None
    pressure = pressures[0] if pressures and all(p is not None and p > 0 for p in pressures) and max(pressures) - min(pressures) < max(1e-6, pressures[0] * 1e-5) else None
    treatments = [canonical((a.get("thermochemistry") or {}).get("thermo_treatment")) for a in species]
    treatment_known = bool(treatments and all(treatments) and len(set(treatments)) == 1)
    if temperature is None:
        warnings.append("Thermochemical temperatures are missing or inconsistent.")
    if pressure is None:
        warnings.append("Thermochemical pressures are missing or inconsistent.")
    if not treatment_known:
        warnings.append("RRHO/quasi-RRHO treatment is missing or inconsistent.")
    reliable = all((a.get("quality") or {}).get("thermochemistry_reliable") is True for a in species)
    if not reliable:
        warnings.append("At least one species lacks verified minimum/thermochemistry checks.")
    thermo_ok = comparable and reliable and temperature is not None and pressure is not None and treatment_known
    e0 = delta(ezpe) if comparable and reliable and treatment_known else None
    h = delta(lambda a: (a.get("thermochemistry") or {}).get("enthalpy_hartree")) if thermo_ok else None
    g = delta(lambda a: (a.get("thermochemistry") or {}).get("gibbs_hartree")) if thermo_ok else None
    standard_states = [canonical((a.get("quality") or {}).get("standard_state")) for a in species]
    standard_known = bool(standard_states and len(set(standard_states)) == 1 and standard_states[0] in
                          ("ideal_gas_at_parsed_pressure", "solution_1m"))
    if not standard_known:
        warnings.append("A common validated standard state is not established; K_eq is withheld.")
    result = {
        "equation": equation, "temperature_K": temperature, "pressure_atm": pressure,
        "delta_e_kj_mol": e * HARTREE_TO_KJMOL if e is not None else None,
        "delta_e0_kj_mol": e0 * HARTREE_TO_KJMOL if e0 is not None else None,
        "delta_h_kj_mol": h * HARTREE_TO_KJMOL if h is not None else None,
        "delta_g_kj_mol": g * HARTREE_TO_KJMOL if g is not None else None,
        "delta_s_j_mol_k": (h - g) * HARTREE_TO_KJMOL * 1000.0 / temperature if h is not None and g is not None and temperature else None,
        "keq": None, "log10_keq": None, "standard_state": standard_states[0] if standard_known else None,
        "warnings": _unique_messages(warnings),
    }
    if g is not None and temperature and standard_known:
        log_keq = -g * HARTREE_TO_KJMOL * 1000.0 / (GAS_CONSTANT_J_MOL_K * temperature)
        result["log10_keq"] = log_keq / math.log(10.0)
        if -700.0 <= log_keq <= 700.0:
            result["keq"] = math.exp(log_keq)
        else:
            result["warnings"].append("K_eq is outside a stable floating-point range; use log10(K_eq).")
    return result


def section_summary(a):
    q = a.get("quality") or {}
    energies, orbitals = a.get("energies") or {}, a.get("orbitals") or {}
    lines = ["File: " + str(a.get("filename")), "Engine: " + str(a.get("engine")) + " " + str(a.get("version") or ""),
             "Method / basis: " + str(a.get("method") or "unrecognized") + " / " + str(a.get("basis") or "unrecognized"),
             "Scientific checks: " + str(q.get("status", "UNVERIFIED")),
             "Stationary point: " + str(a.get("stationary_point_status") or "unverified")]
    for field, label, unit in (("final_energy_hartree", "Electronic energy", "Eh"),):
        v = _finite_number(energies.get(field))
        if v is not None:
            lines.append(label + ": " + format(v, ".10f") + " " + unit)
    for field, label in (("homo_ev", "HOMO"), ("lumo_ev", "LUMO"), ("gap_ev", "Orbital-energy gap")):
        v = _finite_number(orbitals.get(field))
        if v is not None:
            lines.append(label + ": " + format(v, ".6f") + " eV")
    for warning in (a.get("warnings") or [])[:4]:
        lines.append("Warning: " + str(warning)[:300])
    if len(a.get("warnings") or []) > 4:
        lines.append("See Diagnostics/PDF for all warnings.")
    return "\n".join(lines)


def section_diagnostics(a):
    q = a.get("quality") or {}
    fields = ("status", "program_terminated_normally", "scf_converged", "optimization_requested",
              "optimization_converged", "frequency_complete", "geometry_stationarity_verified",
              "stationary_point_status", "thermochemistry_reliable", "standard_state")
    lines = [field.replace("_", " ").capitalize() + ": " + str(q.get(field)) for field in fields]
    lines.extend("Warning: " + str(w) for w in a.get("warnings") or [])
    lines.extend("Program diagnostic: " + str(w) for w in a.get("errors") or [])
    return "\n".join(lines)


def section_structure(a):
    lines = []
    dipole = a.get("dipole") or {}
    value = _finite_number(dipole.get("magnitude_debye")) if isinstance(dipole, dict) else _finite_number(dipole)
    if value is not None:
        lines.append("Dipole magnitude: " + format(value, ".6f") + " debye")
    charges = a.get("atomic_charges") or []
    if charges:
        lines.append("\nAtomic charges (" + str(a.get("atomic_charge_type") or "scheme unrecognized") + "):")
        for atom in charges:
            q = _finite_number(atom.get("charge"))
            if q is not None:
                lines.append(str(atom.get("index")) + " " + str(atom.get("element")) + " " + format(q, ".8f"))
    geometry = a.get("final_geometry") or []
    if geometry:
        lines.append("\nCartesian coordinates (" + str(a.get("geometry_units") or "unverified units") + "):")
        for atom in geometry:
            xyz = [_finite_number(atom.get(axis)) for axis in ("x", "y", "z")]
            if None not in xyz:
                lines.append(str(atom.get("element")) + " " + " ".join(format(v, ".8f") for v in xyz))
    return "\n".join(lines) if lines else "No valid structure/property table was recognized."


# In combined source this wraps the corrected core parser exactly once.
if "parse_output_text" in globals():
    _parse_output_before_validation = parse_output_text
    def parse_output_text(text, filename="calculation.out"):
        return validate_analysis(_parse_output_before_validation(text, filename), source_text=text)

def section_thermo(a, which="all"):
    t=a.get("thermochemistry") or {}
    q=a.get("quality") or {}
    rows=["Thermochemistry checks: "+("VERIFIED AVAILABLE CHECKS" if q.get("thermochemistry_reliable") else "NOT FULLY VERIFIED"),
          "Reference: "+str(q.get("standard_state") or "unverified")]
    for key,label,unit in (("temperature_K","Temperature","K"),("pressure_atm","Pressure","atm")):
        value=_finite_number(t.get(key))
        if value is not None:
            rows.append(label+": "+format(value,".8g")+" "+unit)
    rows.append("Treatment: "+str(t.get("thermo_treatment") or "unrecognized"))
    fields=[
        ("zpe_hartree","Zero-point vibrational energy","Eh","zpe"),
        ("energy_0K_hartree","Electronic energy + ZPE","Eh","zpe"),
        ("electronic_energy_hartree","Thermochemistry electronic reference","Eh","all"),
        ("internal_energy_hartree","Internal energy U","Eh","h"),
        ("thermal_energy_hartree","Total thermal energy","Eh","h"),
        ("enthalpy_hartree","Total enthalpy H","Eh","h"),
        ("gibbs_hartree","Gibbs free energy G","Eh","g"),
        ("entropy_J_mol_K","Entropy S","J mol^-1 K^-1","s"),
    ]
    found=False
    for key,label,unit,group in fields:
        value=_finite_number(t.get(key))
        if value is not None and (which=="all" or group in (which,"all")):
            rows.append(label+": "+format(value,".10g")+" "+unit)
            found=True
    if not found:
        rows.append("The selected thermochemical quantity was not recognized.")
    if q.get("thermochemistry_reliable") is not True:
        rows.extend("Warning: "+str(w) for w in (a.get("warnings") or []))
    return "\n".join(rows)


def section_vibrations(a):
    raw=a.get("frequencies_cm1") or []
    internal=a.get("internal_frequencies_cm1") or []
    q=a.get("quality") or {}
    if not raw:
        return "No verified vibrational-frequency table was recognized."
    lines=["Parsed frequency records: "+str(len(raw)),
           "Internal modes / expected: "+str(len(internal))+" / "+str(a.get("expected_vibration_count")),
           "Hessian complete: "+str(q.get("frequency_complete")),
           "Stationary point: "+str(a.get("stationary_point_status")),
           "Imaginary internal modes below -10 cm^-1: "+str(len(a.get("imaginary_frequencies_cm1") or [])),
           "Small negative internal modes requiring inspection: "+str(len(a.get("near_zero_imaginary_frequencies_cm1") or [])),
           "Raw frequencies (cm^-1; unscaled):",
           ", ".join(format(value,".4f") for value in raw[:500])]
    if len(raw)>500:
        lines.append("The complete frequency table is available in CSV/JSON.")
    lines.append("Mode count alone does not confirm a transition state; inspect displacements and the reaction path.")
    return "\n".join(lines)




"""Scientific reporting appended after the analyzer in the same namespace.

This source is deliberately self-contained: Kaggle receives the entire analyzer
as one Python source string.  No project-relative imports are permitted here.
Only parsed, finite numerical data are rendered; missing intensities are never
replaced by invented equal-height bands.
"""

import csv
import datetime
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

REPORT_GENERATOR_VERSION = "7.1.2"
_RF_HARTREE_TO_KJMOL = 2625.499638
_RF_HC_EV_NM = 1239.841984
_RF_EV_TO_CM1 = 8065.544005
_RF_FWHM_FACTOR = 2.0 * math.sqrt(2.0 * math.log(2.0))
_RF_DEFAULT_PLOT_CONFIG = {
    "ir_sigma_cm1": 12.0,
    "raman_sigma_cm1": 10.0,
    "uv_sigma_ev": 0.15,
    "grid_points": 5500,
    "frequency_scale_factor": 1.0,
    "png_dpi": 600,
    "figure_width_in": 7.0,
    "figure_height_in": 4.5,
}
_RF_COLORS = ("#24567A", "#B64C33", "#568044", "#735890", "#84653C")


def _rf_num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace("D", "E").replace("d", "e"))
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _rf_json_safe(value, omitted=None, location=""):
    """JSON-safe copy, replacing nonfinite numbers with null, never NaN/Inf."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if omitted is not None:
            omitted.append(location or "<root>")
        return None
    if isinstance(value, dict):
        return {str(k): _rf_json_safe(v, omitted, f"{location}.{k}".strip(".")) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rf_json_safe(v, omitted, f"{location}[{i}]") for i, v in enumerate(value)]
    # Numpy scalars may appear when helpers are called programmatically.
    if hasattr(value, "item"):
        try:
            return _rf_json_safe(value.item(), omitted, location)
        except (TypeError, ValueError):
            pass
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _rf_json_write(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_rf_json_safe(data), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return str(path)


def _rf_csv_write(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if item is None else item for item in row])
    return str(path)


def _rf_safe_base(value, fallback="calculation"):
    name = Path(str(value or fallback)).stem
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return name[:120] or fallback


def _rf_config(analysis=None):
    config = dict(_RF_DEFAULT_PLOT_CONFIG)
    raw = (analysis or {}).get("plot_config", {})
    if isinstance(raw, dict):
        for key in ("ir_sigma_cm1", "raman_sigma_cm1", "uv_sigma_ev", "frequency_scale_factor"):
            number = _rf_num(raw.get(key))
            if number is not None and number > 0:
                config[key] = number
        number = _rf_num(raw.get("grid_points"))
        if number is not None:
            config["grid_points"] = max(2000, min(20000, int(number)))
    # Publication outputs always meet the promised resolution.
    config["png_dpi"] = 600
    return config


def _rf_style():
    return {
        "font.family": "DejaVu Sans",
        "font.size": 8.5,
        "axes.labelsize": 9,
        "axes.titlesize": 10,
        "axes.linewidth": 0.65,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "legend.fontsize": 7.5,
        "legend.frameon": False,
        "lines.linewidth": 1.25,
        "savefig.facecolor": "white",
        "figure.facecolor": "white",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "axes.spines.top": False,
        "axes.spines.right": False,
    }


def _rf_plot_imports():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    return plt, np


def _rf_synthetic(analysis):
    provenance = (analysis or {}).get("provenance", {})
    return bool((analysis or {}).get("synthetic_demo") or (analysis or {}).get("is_synthetic") or
                (isinstance(provenance, dict) and "synthetic" in str(provenance.get("data_origin", "")).lower()))


def _rf_plot_note(fig, analysis, note=""):
    lines = []
    if _rf_synthetic(analysis):
        lines.append("SYNTHETIC VALIDATION EXAMPLE")
    else:
        quality = (analysis or {}).get("quality", {}) or {}
        validation = quality.get("status", "UNVERIFIED") if isinstance(quality, dict) else "UNVERIFIED"
        if str(validation).upper() != "PASS":
            lines.append("Validation: " + str(validation).upper())
    if note:
        lines.append(note)
    if lines:
        fig.text(0.5, 0.016, "  |  ".join(lines), ha="center", va="bottom", fontsize=6.5, color="#444444")
    fig.tight_layout(rect=(0, 0.045 if lines else 0, 1, 1))


def _rf_gaussian_density(points, grid, sigma, positive_domain=True):
    """Area equals sum(stick weights) on the full positive coordinate domain.

    A stick of weight w has w/(sigma*sqrt(2*pi)) times a Gaussian.  When the
    physical domain is positive, its normalizing factor is Phi(center/sigma).
    Finite exported grids capture a documented fraction of the total area.
    """
    import numpy as np
    width = _rf_num(sigma)
    if width is None or width <= 0:
        raise ValueError("Gaussian sigma must be positive and finite.")
    x = np.asarray(grid, dtype=float)
    result = np.zeros_like(x)
    for center, weight in points:
        center = _rf_num(center)
        weight = _rf_num(weight)
        if center is None or weight is None or weight < 0 or (positive_domain and center <= 0):
            continue
        denominator = width * math.sqrt(2.0 * math.pi)
        if positive_domain:
            denominator *= 0.5 * (1.0 + math.erf(center / (width * math.sqrt(2.0))))
        result += (weight / denominator) * np.exp(-0.5 * ((x - center) / width) ** 2)
    if positive_domain:
        result[x < 0] = 0.0
    if not np.all(np.isfinite(result)):
        raise ValueError("Gaussian density exceeds the finite numerical range; verify sigma and stick strengths.")
    return result


def _rf_captured_area(points, low, high, sigma):
    captured = 0.0
    total = 0.0
    for center, weight in points:
        if center <= 0 or weight < 0:
            continue
        z = 0.5 * (1.0 + math.erf(center / (sigma * math.sqrt(2.0))))
        cdf_high = 0.5 * (1.0 + math.erf((high - center) / (sigma * math.sqrt(2.0))))
        cdf_low = 0.5 * (1.0 + math.erf((max(0.0, low) - center) / (sigma * math.sqrt(2.0))))
        captured += weight * (cdf_high - cdf_low) / z
        total += weight
    return captured / total if total > 0 else None


def _rf_spectrum_points(analysis, kind, scale=1.0):
    key = "raman_spectrum" if kind == "raman" else "ir_spectrum"
    points, omitted = [], 0
    for item in (analysis or {}).get(key, []) or []:
        if isinstance(item, dict):
            x = next((_rf_num(item[k]) for k in ("frequency_cm1", "frequency_cm", "freq", "cm1") if item.get(k) is not None), None)
            ykeys = ("activity", "raman_activity", "intensity") if kind == "raman" else ("intensity_km_mol", "intensity")
            y = next((_rf_num(item[k]) for k in ykeys if item.get(k) is not None), None)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = _rf_num(item[0]), _rf_num(item[1])
        else:
            x = y = None
        if x is None or y is None or x <= 0 or y < 0:
            omitted += 1
            continue
        scaled = _rf_num(x * scale)
        if scaled is None:
            omitted += 1
            continue
        points.append((scaled, y))
    return points, omitted


def _rf_transitions(analysis):
    states, omitted = [], 0
    for number, item in enumerate((analysis or {}).get("tddft_states", []) or [], 1):
        if not isinstance(item, dict):
            omitted += 1
            continue
        energy = _rf_num(item.get("ev"))
        printed_nm = _rf_num(item.get("nm"))
        cm1 = _rf_num(item.get("cm1"))
        weight = _rf_num(item.get("f"))
        if energy is None and cm1 is not None and cm1 > 0:
            energy = cm1 / _RF_EV_TO_CM1
        if energy is None and printed_nm is not None and printed_nm > 0:
            energy = _RF_HC_EV_NM / printed_nm
        energy = _rf_num(energy)
        if energy is None or energy <= 0 or weight is None or weight < 0:
            omitted += 1
            continue
        wavelength = _rf_num(_RF_HC_EV_NM / energy)
        if wavelength is None or wavelength <= 0:
            omitted += 1
            continue
        states.append({"state": item.get("state", number), "ev": energy,
                       "nm": wavelength, "printed_nm": printed_nm, "f": weight})
    return states, omitted


def _rf_export_figure(fig, basepath, metadata, analysis=None):
    """Save an editable SVG, vector PDF, 600 dpi PNG and finite JSON metadata."""
    plt, _ = _rf_plot_imports()
    basepath = os.path.abspath(str(basepath))
    os.makedirs(os.path.dirname(basepath), exist_ok=True)
    settings = dict(metadata)
    settings.update({"report_generator_version": REPORT_GENERATOR_VERSION, "png_dpi": 600,
                     "svg_text_is_editable": True, "vector_pdf_fonts_embedded": True,
                     "source_sha256": (analysis or {}).get("source_sha256") or ((analysis or {}).get("provenance") or {}).get("source_sha256"),
                     "software_versions": ((analysis or {}).get("report_provenance") or {}).get("software_versions"),
                     "synthetic_validation_example": _rf_synthetic(analysis)})
    try:
        for extension in (".png", ".svg", ".pdf"):
            # Publish each complete file atomically. Readers must never see a
            # partially written PNG when two workers share an output directory.
            descriptor, temporary_path = tempfile.mkstemp(prefix=".figure-", suffix=extension, dir=os.path.dirname(basepath))
            os.close(descriptor)
            try:
                fig.savefig(temporary_path, format=extension[1:], dpi=600 if extension == ".png" else None,
                            bbox_inches="tight", facecolor="white")
                os.replace(temporary_path, basepath + extension)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        _rf_json_write(basepath + "_plot_settings.json", settings)
    finally:
        plt.close(fig)
    return basepath + ".png"


def _rf_frontier_channels(analysis):
    """Use actual finite orbital rows only, never fabricate missing MO records."""
    section = (analysis or {}).get("orbitals", {}) or {}
    if not isinstance(section, dict):
        return {}
    groups = {}
    for row in section.get("orbitals", []) or []:
        if not isinstance(row, dict) or row.get("_synthetic"):
            continue
        occ, energy, hartree = _rf_num(row.get("occ")), _rf_num(row.get("ev")), _rf_num(row.get("eh"))
        if energy is None and hartree is not None:
            energy = _rf_num(hartree * 27.211386245988)
        if occ is None or energy is None or occ < 0 or occ > 2.0001:
            continue
        spin = str(row.get("spin") or "restricted").lower().strip()
        if spin in ("alpha", "a", "up", "spin-up", "spin up"):
            spin = "alpha"
        elif spin in ("beta", "b", "down", "spin-down", "spin down"):
            spin = "beta"
        else:
            spin = "restricted"
        if spin != "restricted" and occ > 1.0001:
            continue
        groups.setdefault(spin, []).append(dict(row, occ=occ, ev=energy, eh=hartree, spin=spin))
    if "alpha" in groups or "beta" in groups:
        groups.pop("restricted", None)
    return {name: sorted(rows, key=lambda r: r["ev"]) for name, rows in groups.items()}


def _rf_frontier_records(analysis, each_side=3):
    records = []
    for spin, rows in _rf_frontier_channels(analysis).items():
        occupied = [r for r in rows if r["occ"] > 1e-8]
        virtual = [r for r in rows if r["occ"] <= 1e-8]
        for i, row in enumerate(occupied[-each_side:]):
            distance = len(occupied[-each_side:]) - i - 1
            records.append((spin, "HOMO" if distance == 0 else f"HOMO-{distance}", row))
        for i, row in enumerate(virtual[:each_side]):
            records.append((spin, "LUMO" if i == 0 else f"LUMO+{i}", row))
    return records


def _rf_level_labels(energies, low, high):
    """Spread text labels; connector endpoints preserve the actual energies."""
    if not energies:
        return []
    gap = (high - low) / max(8.0, 1.6 * len(energies))
    positions = [max(low, min(high, energies[0]))]
    for energy in energies[1:]:
        positions.append(max(energy, positions[-1] + gap))
    if positions[-1] > high:
        excess = positions[-1] - high
        positions = [p - excess for p in positions]
    return positions


def make_plots(a, outdir):
    """Return preview PNG paths; vectors, numerical CSVs and settings accompany them."""
    outdir = os.path.abspath(str(outdir))
    os.makedirs(outdir, exist_ok=True)
    plt, np = _rf_plot_imports()
    config = _rf_config(a)
    made = {}
    size = (config["figure_width_in"], config["figure_height_in"])
    with plt.rc_context(_rf_style()):
        energies = [(i + 1, _rf_num(value)) for i, value in enumerate(a.get("optimization_energies", []) or [])]
        energies = [(i, value) for i, value in energies if value is not None]
        if len(energies) >= 2:
            minimum = min(value for _, value in energies)
            rows = [(i, value, (value - minimum) * _RF_HARTREE_TO_KJMOL) for i, value in energies]
            base = os.path.join(outdir, "optimization_energy")
            _rf_csv_write(base + ".csv", ["parsed_cycle", "energy_hartree", "relative_energy_kj_mol"], rows)
            fig, ax = plt.subplots(figsize=size)
            ax.plot([r[0] for r in rows], [r[2] for r in rows], "o-", color=_RF_COLORS[0], markersize=3)
            ax.set(xlabel="Parsed optimization cycle", ylabel=r"Relative energy (kJ mol$^{-1}$)", title="Optimization energy profile")
            ax.grid(axis="y", color="#DEDEDE", linewidth=0.4)
            _rf_plot_note(fig, a, "Reference: lowest parsed optimization energy")
            made["optimization"] = _rf_export_figure(fig, base, {"kind": "optimization", "reference_energy_hartree": minimum}, a)

        for kind, key in (("ir", "ir"), ("raman", "raman")):
            points, omitted = _rf_spectrum_points(a, kind, config["frequency_scale_factor"])
            if not points or not any(weight > 0 for _, weight in points):
                continue
            sigma = config["ir_sigma_cm1" if kind == "ir" else "raman_sigma_cm1"]
            low, high = max(0.0, min(x for x, _ in points) - 6 * sigma), max(x for x, _ in points) + 6 * sigma
            grid = np.linspace(low, high, config["grid_points"])
            density = _rf_gaussian_density(points, grid, sigma)
            base = os.path.join(outdir, kind + "_spectrum")
            weight_name = "ir_intensity_km_mol" if kind == "ir" else "raman_activity_angstrom4_u"
            _rf_csv_write(base + "_sticks.csv", ["frequency_cm1", weight_name], points)
            _rf_csv_write(base + "_curve.csv", ["wavenumber_cm1", weight_name + "_per_cm1"], zip(grid.tolist(), density.tolist()))
            fig, ax = plt.subplots(figsize=size)
            ax.plot(grid, density, color=_RF_COLORS[0])
            ax.set_xlabel(r"Wavenumber (cm$^{-1}$)" if kind == "ir" else r"Raman shift (cm$^{-1}$)")
            unit_label = r"IR intensity density (km mol$^{-1}$ per cm$^{-1}$)" if kind == "ir" else r"Raman activity density ($\AA^4$ u$^{-1}$ per cm$^{-1}$)"
            ax.set_ylabel(unit_label)
            ax.set_title("Calculated IR spectrum" if kind == "ir" else "Calculated Raman activity profile")
            ax.set_xlim(high, low)
            ax.set_ylim(bottom=0)
            scaling = "Unscaled frequencies" if config["frequency_scale_factor"] == 1 else f"Frequency scale factor: {config['frequency_scale_factor']:g}"
            _rf_plot_note(fig, a, f"{scaling}; Gaussian sigma = {sigma:g} cm-1; FWHM = {sigma*_RF_FWHM_FACTOR:.2f} cm-1")
            metadata = {"kind": kind, "gaussian_domain": "wavenumber_cm1", "sigma": sigma, "sigma_units": "cm^-1",
                        "fwhm": sigma * _RF_FWHM_FACTOR, "normalization": "area-preserving positive-domain Gaussian",
                        "ordinate_units": "km mol^-1 / (cm^-1)" if kind == "ir" else "angstrom^4 u^-1 / (cm^-1)",
                        "frequency_scale_factor": config["frequency_scale_factor"], "domain_cm1": [low, high],
                        "displayed_fraction_of_total_area": _rf_captured_area(points, low, high, sigma), "omitted_nonphysical_or_missing_records": omitted}
            made[key] = _rf_export_figure(fig, base, metadata, a)

        transitions, omitted = _rf_transitions(a)
        if transitions and any(state["f"] > 0 for state in transitions):
            sigma = config["uv_sigma_ev"]
            points = [(state["ev"], state["f"]) for state in transitions]
            low = max(1e-5, min(x for x, _ in points) - 6 * sigma)
            high = max(x for x, _ in points) + 6 * sigma
            energy = np.linspace(low, high, config["grid_points"])
            density_ev = _rf_gaussian_density(points, energy, sigma)
            _rf_csv_write(os.path.join(outdir, "uvvis_transitions.csv"), ["state", "excitation_energy_ev", "wavelength_nm_from_energy", "printed_wavelength_nm", "oscillator_strength"],
                          [(s["state"], s["ev"], s["nm"], s["printed_nm"], s["f"]) for s in transitions])
            _rf_csv_write(os.path.join(outdir, "uvvis_energy_curve.csv"), ["energy_ev", "oscillator_strength_density_per_ev"], zip(energy.tolist(), density_ev.tolist()))
            for domain, name in (("energy", "uvvis_energy"), ("wavelength", "uvvis")):
                fig, ax = plt.subplots(figsize=size)
                metadata = {"kind": "uvvis", "gaussian_domain": "energy_ev", "sigma": sigma, "sigma_units": "eV",
                            "fwhm": sigma * _RF_FWHM_FACTOR, "normalization": "area-preserving positive-energy Gaussian",
                            "energy_to_wavelength_constant_ev_nm": _RF_HC_EV_NM, "omitted_nonphysical_or_missing_records": omitted}
                if domain == "energy":
                    x, y = energy, density_ev
                    ax.set(xlabel="Excitation energy (eV)", ylabel=r"Oscillator-strength density (eV$^{-1}$)")
                    metadata.update({"display_domain": "energy_ev", "display_energy_domain_ev": [low, high],
                                     "displayed_fraction_of_total_area": _rf_captured_area(points, low, high, sigma)})
                else:
                    # Bound the viewing domain, not the available transitions. All
                    # transition wavelengths are included, including near infrared.
                    view_low = max(min(x for x, _ in points) * 0.2, min(x for x, _ in points) - 3.5 * sigma)
                    view_high = max(x for x, _ in points) + 3.5 * sigma
                    selected = (energy >= view_low) & (energy <= view_high)
                    e_selected = energy[selected]
                    x = (_RF_HC_EV_NM / e_selected)[::-1]
                    y = (density_ev[selected] * (e_selected ** 2 / _RF_HC_EV_NM))[::-1]
                    ax.set(xlabel="Wavelength (nm)", ylabel=r"Oscillator-strength density (nm$^{-1}$)")
                    metadata.update({"display_domain": "wavelength_nm", "display_energy_domain_ev": [view_low, view_high],
                                     "wavelength_transform": "lambda=hc/E; density_lambda=density_E*hc/lambda^2",
                                     "displayed_fraction_of_total_area": _rf_captured_area(points, view_low, view_high, sigma)})
                    _rf_csv_write(os.path.join(outdir, "uvvis_wavelength_curve.csv"), ["wavelength_nm", "oscillator_strength_density_per_nm"], zip(x.tolist(), y.tolist()))
                ax.plot(x, y, color=_RF_COLORS[0])
                ax.set(title="Calculated electronic absorption profile")
                ax.set_xlim(float(x.min()), float(x.max()))
                ax.set_ylim(bottom=0)
                _rf_plot_note(fig, a, f"Energy-domain Gaussian sigma = {sigma:g} eV; FWHM = {sigma*_RF_FWHM_FACTOR:.3f} eV")
                made[name] = _rf_export_figure(fig, os.path.join(outdir, name + "_spectrum"), metadata, a)

        records = _rf_frontier_records(a, each_side=3)
        if records:
            spin_names = [spin for spin in ("restricted", "alpha", "beta") if any(row[0] == spin for row in records)]
            values = [row[2]["ev"] for row in records]
            span = max(2.0, max(values) - min(values))
            low, high = min(values) - 0.15 * span, max(values) + 0.15 * span
            fig, ax = plt.subplots(figsize=size)
            for channel, spin in enumerate(spin_names):
                selected = sorted([r for r in records if r[0] == spin], key=lambda r: r[2]["ev"])
                anchors = _rf_level_labels([r[2]["ev"] for r in selected], low + 0.06 * span, high - 0.06 * span)
                center = channel * 1.7
                for (_, label, row), anchor in zip(selected, anchors):
                    color = _RF_COLORS[0] if row["occ"] > 1e-8 else _RF_COLORS[1]
                    ax.hlines(row["ev"], center - 0.23, center + 0.15, color=color, linewidth=1.4)
                    ax.plot([center + 0.15, center + 0.28], [row["ev"], anchor], color="#777777", linewidth=0.5)
                    ax.text(center + 0.3, anchor, f"{label}: {row['ev']:.3f}", va="center", fontsize=7.5)
            ax.set(ylim=(low, high), xlim=(-0.55, (len(spin_names)-1)*1.7 + 1.25), ylabel="Orbital energy (eV)", title="Frontier orbital energies")
            ax.set_xticks([i * 1.7 for i in range(len(spin_names))], [s.capitalize() for s in spin_names])
            ax.tick_params(axis="x", length=0)
            ax.grid(axis="y", color="#E0E0E0", linewidth=0.4)
            _rf_plot_note(fig, a, "Energy levels from parsed MO data; no orbital isosurfaces")
            base = os.path.join(outdir, "frontier_orbitals")
            _rf_csv_write(base + ".csv", ["spin", "frontier_label", "parsed_mo_index", "occupation", "energy_hartree", "energy_ev"],
                          [(spin, label, row.get("index"), row["occ"], row.get("eh"), row["ev"]) for spin, label, row in records])
            made["orbitals"] = _rf_export_figure(fig, base, {"kind": "frontier_orbitals", "spin_channels": spin_names,
                                               "frontier_window_each_side": 3, "missing_or_synthetic_rows_are_not_drawn": True}, a)
    return made


def make_overlay_plot(analyses, kind, outpath, normalize=True, sigma=None):
    """Compare finite parsed spectra. UV sigma is in eV; IR sigma is in cm^-1.

    UV is broadened in energy and transformed with the Jacobian to wavelength.
    Raw mode preserves amplitudes; normalized mode divides each density by its
    own peak height and uses an explicit dimensionless relative-intensity axis.
    """
    is_uv = str(kind).lower() in ("uv", "uvvis", "td", "tddft")
    is_raman = str(kind).lower() == "raman"
    width = _rf_num(sigma if sigma is not None else (0.15 if is_uv else (10.0 if is_raman else 12.0)))
    if width is None or width <= 0:
        raise ValueError("Gaussian sigma must be positive and finite; UV uses eV, IR/Raman use cm^-1.")
    plt, np = _rf_plot_imports()
    prepared, names = [], {}
    for analysis in analyses:
        if is_uv:
            states, omitted = _rf_transitions(analysis)
            points = [(row["ev"], row["f"]) for row in states]
        else:
            points, omitted = _rf_spectrum_points(analysis, "raman" if is_raman else "ir", _rf_config(analysis)["frequency_scale_factor"])
        if not points or not any(weight > 0 for _, weight in points):
            continue
        name = Path(str(analysis.get("filename", "spectrum"))).stem
        names[name] = names.get(name, 0) + 1
        label = name if names[name] == 1 else f"{name} [{names[name]}]"
        prepared.append((analysis, label, points, omitted))
    if len(prepared) < 2:
        return None
    allpoints = [point for _, _, points, _ in prepared for point in points]
    if is_uv:
        low = max(min(x for x, _ in allpoints) * 0.2, min(x for x, _ in allpoints) - 3.5 * width)
        high = max(x for x, _ in allpoints) + 3.5 * width
    else:
        low, high = max(0.0, min(x for x, _ in allpoints) - 6 * width), max(x for x, _ in allpoints) + 6 * width
    grid = np.linspace(low, high, 5500)
    curve_rows = []
    metadata = {"kind": "uvvis_overlay" if is_uv else ("raman_overlay" if is_raman else "ir_overlay"),
                "sigma": width, "sigma_units": "eV" if is_uv else "cm^-1", "fwhm": width * _RF_FWHM_FACTOR,
                "gaussian_domain": "energy_ev" if is_uv else "wavenumber_cm1", "positive_domain_area_preserving": True,
                "display_coordinate": "wavelength_nm" if is_uv else "wavenumber_cm1", "normalize_to_individual_maximum": bool(normalize),
                "ordinate_units": "dimensionless" if normalize else ("nm^-1" if is_uv else ("angstrom^4 u^-1 / (cm^-1)" if is_raman else "km mol^-1 / (cm^-1)")),
                "spectra": []}
    with plt.rc_context(_rf_style()):
        fig, ax = plt.subplots(figsize=(7, 4.5))
        try:
            for i, (analysis, label, points, omitted) in enumerate(prepared):
                raw = _rf_gaussian_density(points, grid, width)
                if is_uv:
                    x = (_RF_HC_EV_NM / grid)[::-1]
                    density = (raw * grid ** 2 / _RF_HC_EV_NM)[::-1]
                else:
                    x, density = grid, raw
                if normalize and density.max() > 0:
                    density = density / density.max()
                ax.plot(x, density, label=label, color=_RF_COLORS[i % len(_RF_COLORS)], linestyle=("-", "--", "-.")[i // len(_RF_COLORS) % 3])
                curve_rows.append(density)
                metadata["spectra"].append({"label": label, "filename": analysis.get("filename"), "source_sha256": analysis.get("source_sha256"),
                                             "omitted_records": omitted, "frequency_scale_factor": _rf_config(analysis)["frequency_scale_factor"] if not is_uv else None,
                                             "displayed_fraction_of_total_area": _rf_captured_area(points, low, high, width)})
            if is_uv:
                ylabel = "Relative intensity (unitless)" if normalize else r"Oscillator-strength density (nm$^{-1}$)"
                ax.set(xlabel="Wavelength (nm)", ylabel=ylabel, title="Electronic absorption comparison", xlim=(float(x.min()), float(x.max())))
                metadata["wavelength_transform"] = "lambda=hc/E; density_lambda=density_E*hc/lambda^2"
            else:
                raw_label = r"Raman activity density ($\AA^4$ u$^{-1}$ per cm$^{-1}$)" if is_raman else r"IR intensity density (km mol$^{-1}$ per cm$^{-1}$)"
                ax.set(xlabel=r"Wavenumber (cm$^{-1}$)", ylabel="Relative intensity (unitless)" if normalize else raw_label,
                       title="Raman activity comparison" if is_raman else "Calculated IR comparison", xlim=(high, low))
            ax.set_ylim(bottom=0)
            ax.legend(loc="best")
            units = "eV" if is_uv else "cm-1"
            validation_states = [str((row[0].get("quality") or {}).get("status", "UNVERIFIED")).upper() for row in prepared]
            aggregate = "FAIL" if "FAIL" in validation_states else ("UNVERIFIED" if "UNVERIFIED" in validation_states else ("WARNING" if "WARNING" in validation_states else "PASS"))
            demo = {"synthetic_demo": any(_rf_synthetic(row[0]) for row in prepared), "quality": {"status": aggregate}}
            metadata["validation_status"] = aggregate
            _rf_plot_note(fig, demo, f"Gaussian sigma = {width:g} {units}; FWHM = {width*_RF_FWHM_FACTOR:.3f} {units}; {'peak normalized' if normalize else 'raw amplitude'}")
            base = str(Path(os.path.abspath(str(outpath))).with_suffix(""))
            os.makedirs(os.path.dirname(base), exist_ok=True)
            header = ["wavelength_nm" if is_uv else "wavenumber_cm1"] + ["spectrum_" + str(i+1) for i in range(len(curve_rows))]
            _rf_csv_write(base + "_curve.csv", header, zip(x.tolist(), *[row.tolist() for row in curve_rows]))
            metadata["csv_columns"] = {header[i+1]: row[1] for i, row in enumerate(prepared)}
            return _rf_export_figure(fig, base, metadata, demo)
        finally:
            plt.close(fig)


def _rf_warnings(analysis):
    quality = analysis.get("quality", {}) or {}
    sources = [analysis.get("warnings", []), quality.get("warnings", []) if isinstance(quality, dict) else [], analysis.get("errors", [])]
    result = []
    for source in sources:
        source = [source] if isinstance(source, str) else source or []
        for item in source:
            message = str(item).strip()
            if message and message not in result:
                result.append(message)
    return result


def _rf_fmt(value, digits=6):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "Unverified / unavailable"
    number = _rf_num(value)
    if number is not None:
        return f"{number:.{digits}f}"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_rf_json_safe(value), ensure_ascii=False, allow_nan=False)
    return str(value)


def _rf_pdf_font_names():
    from matplotlib import font_manager
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    regular, bold = "ChemReportRegular", "ChemReportBold"
    for name, weight in ((regular, "normal"), (bold, "bold")):
        if name not in pdfmetrics.getRegisteredFontNames():
            filename = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans", weight=weight))
            pdfmetrics.registerFont(TTFont(name, filename))
    pdfmetrics.registerFontFamily(regular, normal=regular, bold=bold, italic=regular, boldItalic=bold)
    return regular, bold


def build_pdf(a, plots, pdf_path):
    """Scientific numerical report with explicit validation and provenance."""
    a = _rf_json_safe(a)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, Image, KeepTogether
    regular, bold = _rf_pdf_font_names()
    body = ParagraphStyle("ReportBody7", fontName=regular, fontSize=8.2, leading=11)
    small = ParagraphStyle("ReportSmall7", parent=body, fontSize=7.2, leading=9.6, textColor=colors.HexColor("#444444"))
    heading = ParagraphStyle("ReportHeading7", parent=body, fontName=bold, fontSize=11, leading=14, spaceBefore=12, spaceAfter=5, keepWithNext=True)
    title = ParagraphStyle("ReportTitle7", parent=heading, fontSize=18, leading=22, spaceBefore=0, spaceAfter=8)
    label = ParagraphStyle("ReportTableHeading7", parent=small, fontName=bold)
    def esc(value):
        return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def table(rows, widths=None):
        cooked = [[Paragraph(esc(item), label if i == 0 else small) for item in row] for i, row in enumerate(rows)]
        item = LongTable(cooked, colWidths=widths or [8.0*cm, 9.0*cm], repeatRows=1, hAlign="LEFT")
        item.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"), ("LINEABOVE",(0,0),(-1,0),0.6,colors.black),
                                 ("LINEBELOW",(0,0),(-1,0),0.4,colors.black), ("LINEBELOW",(0,-1),(-1,-1),0.4,colors.black),
                                 ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
                                 ("LEFTPADDING",(0,0),(-1,-1),4), ("RIGHTPADDING",(0,0),(-1,-1),4)]))
        return item
    filename = str(a.get("filename") or "calculation.out")
    quality = a.get("quality", {}) or {}
    quality = quality if isinstance(quality, dict) else {}
    status = str(quality.get("status", "UNVERIFIED"))
    story = [Paragraph("Computational chemistry report", title), Paragraph("Source file: " + esc(filename), body)]
    if _rf_synthetic(a):
        story += [Paragraph("SYNTHETIC VALIDATION EXAMPLE", heading), Paragraph("The values in this example are synthetic and demonstrate report generation.", body)]
    story += [Spacer(1, 6), table([["Validation check", "Result"], ["Overall validation", status],
                    ["Program terminated normally", _rf_fmt(quality.get("program_terminated_normally", a.get("normal_termination")))],
                    ["SCF converged", _rf_fmt(quality.get("scf_converged"))],
                    ["Optimization requested", _rf_fmt(quality.get("optimization_requested"))],
                    ["Optimization converged", _rf_fmt(quality.get("optimization_converged"))],
                    ["Frequency calculation complete", _rf_fmt(quality.get("frequency_complete"))],
                    ["Stationary-point assessment", quality.get("stationary_point_status", a.get("stationary_point_status", "Unverified"))],
                    ["Thermochemistry verified", _rf_fmt(quality.get("thermochemistry_reliable"))]])]
    warnings = _rf_warnings(a)
    if status.upper() != "PASS":
        story += [Spacer(1, 6), Paragraph("Interpretation requires the validation checks above. Parsed values from incomplete or unconverged calculations remain unverified.", body)]
    if warnings:
        story += [Paragraph("Warnings and limitations", heading)]
        for message in warnings:
            story.append(Paragraph(esc(message), small))
            story.append(Spacer(1, 3))
    story += [Paragraph("Calculation and provenance", heading)]
    provenance = a.get("provenance", {}) or {}
    provenance = provenance if isinstance(provenance, dict) else {}
    metadata_rows = [["Field", "Value"], ["Program / version", f"{a.get('engine') or 'Unverified'} / {a.get('version') or 'Unverified'}"],
                     ["Method", a.get("method") or "Unverified"], ["Basis", a.get("basis") or "Unverified"],
                     ["Solvation model / solvent", f"{a.get('solvation_model') or 'Unverified'} / {a.get('solvent') or 'Unverified'}"],
                     ["Total charge / multiplicity", f"{_rf_fmt((a.get('system') or {}).get('charge'),0)} / {_rf_fmt((a.get('system') or {}).get('multiplicity'),0)}"],
                     ["Source SHA-256", a.get("source_sha256") or provenance.get("source_sha256") or "Unavailable"],
                     ["Source hash scope", a.get("source_hash_scope") or provenance.get("source_hash_scope") or "Unspecified"],
                     ["Parser / schema", f"{a.get('parser_version') or 'Unverified'} / {a.get('analysis_schema_version') or 'Unverified'}"],
                     ["Report generator", REPORT_GENERATOR_VERSION]]
    story.append(table(metadata_rows))
    energies = a.get("energies", {}) or {}
    thermochemistry = a.get("thermochemistry", {}) or {}
    story += [Paragraph("Electronic energies", heading)]
    rows = [["Quantity", "Value", "Unit"]]
    for key, name in (("final_energy_hartree", "Final electronic energy"), ("reference_energy_hartree", "Reference energy"),
                      ("scf_energy_hartree", "SCF energy"), ("correlation_energy_hartree", "Correlation energy"),
                      ("dispersion_energy_hartree", "Dispersion correction"), ("nuclear_repulsion_hartree", "Nuclear repulsion energy")):
        if _rf_num(energies.get(key)) is not None:
            rows.append([name, _rf_fmt(energies[key], 10), "hartree"])
    if len(rows) > 1:
        story.append(table(rows, [8.5*cm, 5.0*cm, 3.5*cm]))
    else:
        story.append(Paragraph("A finite final electronic energy was not available in the parsed output.", body))
    if thermochemistry:
        story += [Paragraph("Thermochemistry", heading)]
        if quality.get("thermochemistry_reliable") is not True:
            story.append(Paragraph("These thermochemical values are unverified for quantitative interpretation.", body))
        rows = [["Quantity", "Value", "Unit"]]
        thermo_fields = [("temperature_K", "Temperature", "K", 3), ("pressure_atm", "Pressure", "atm", 6),
                        ("zpe_hartree", "Zero-point energy", "hartree", 8), ("energy_0K_hartree", "Electronic energy + ZPE", "hartree", 10),
                        ("thermal_energy_correction_hartree", "Thermal energy correction", "hartree", 8),
                        ("enthalpy_correction_hartree", "Enthalpy correction (H-Eel)", "hartree", 8), ("gibbs_correction_hartree", "Gibbs correction (G-Eel)", "hartree", 8),
                        ("internal_energy_hartree", "Internal energy", "hartree", 10), ("enthalpy_hartree", "Total enthalpy", "hartree", 10),
                        ("gibbs_hartree", "Gibbs free energy", "hartree", 10), ("entropy_J_mol_K", "Entropy", "J mol−1 K−1", 5)]
        for key, name, unit, digits in thermo_fields:
            if _rf_num(thermochemistry.get(key)) is not None:
                rows.append([name, _rf_fmt(thermochemistry[key], digits), unit])
        if len(rows) > 1:
            story.append(table(rows, [8.5*cm, 5.0*cm, 3.5*cm]))
        standard_state = quality.get("standard_state", a.get("standard_state"))
        story.append(Paragraph("Standard state: " + esc(_rf_fmt(standard_state)), small))
    frequencies = [number for value in a.get("frequencies_cm1", []) or [] if (number := _rf_num(value)) is not None]
    if frequencies or a.get("ir_spectrum") or a.get("raman_spectrum"):
        story += [Paragraph("Vibrational data", heading)]
        threshold = _rf_num(quality.get("imaginary_noise_threshold_cm1")) or 10.0
        story.append(table([["Quantity", "Value"], ["Parsed frequency records", str(len(frequencies))],
                            ["Negative frequencies", str(sum(value < 0 for value in frequencies))],
                            [f"Negative frequencies below −{threshold:g} cm−1", str(sum(value < -threshold for value in frequencies))],
                            ["Validated vibration count / expected", f"{quality.get('vibration_count', 'Unverified')} / {quality.get('expected_vibration_count', 'Unverified')}"],
                            ["Frequency scaling factor for plots", _rf_fmt(_rf_config(a)["frequency_scale_factor"], 6)]]))
        story.append(Paragraph("Frequencies are unscaled unless an explicit plot scale factor is recorded. Imaginary-mode counts alone do not establish a transition state; geometry convergence and a complete Hessian require verification.", small))
        for kind, name, unit in (("ir", "Strongest parsed IR bands", "km mol−1"), ("raman", "Strongest parsed Raman activities", "Å4 u−1")):
            points, omitted = _rf_spectrum_points(a, kind)
            positive = [(x, y) for x, y in points if y > 0]
            if positive:
                rows = [["Frequency (cm−1)", "IR intensity (" + unit + ")" if kind == "ir" else "Raman activity (" + unit + ")"]]
                rows += [[_rf_fmt(x, 3), _rf_fmt(y, 6)] for x, y in sorted(positive, key=lambda p: p[1], reverse=True)[:12]]
                story += [Paragraph(name, heading), table(rows)]
            elif (kind == "ir" and frequencies) or a.get(kind + "_spectrum"):
                story.append(Paragraph("No positive finite " + ("IR intensities" if kind == "ir" else "Raman activities") + " were parsed; the corresponding spectrum is unavailable.", small))
            if omitted:
                story.append(Paragraph(f"{omitted} {kind.upper()} entries with missing, nonfinite or nonphysical values were omitted from rendering.", small))
    transitions, omitted = _rf_transitions(a)
    if transitions:
        story += [Paragraph("Electronic transitions", heading)]
        rows = [["State", "Energy (eV)", "Wavelength (nm)", "Oscillator strength"]]
        rows += [[state["state"], _rf_fmt(state["ev"], 5), _rf_fmt(state["nm"], 3), _rf_fmt(state["f"], 7)] for state in transitions[:40]]
        story.append(table(rows, [1.8*cm, 4.7*cm, 4.8*cm, 5.7*cm]))
        story.append(Paragraph("Wavelengths are computed from parsed excitation energies. Absorption profiles use a Gaussian in energy; the wavelength density includes the Jacobian hc/λ². The ordinate is oscillator-strength density, rather than a calibrated molar absorption coefficient.", small))
        if len(transitions) > 40:
            story.append(Paragraph("The table shows the first 40 transitions; the complete numerical export is provided as CSV and JSON.", small))
        if not any(state["f"] > 0 for state in transitions):
            story.append(Paragraph("All parsed oscillator strengths are zero; no absorption profile was generated.", small))
    elif a.get("tddft_states"):
        story += [Paragraph("Electronic transitions", heading), Paragraph("Positive finite excitation energies and finite nonnegative oscillator strengths were not jointly available; an absorption profile could not be generated.", body)]
    orbitals = a.get("orbitals", {}) or {}
    records = _rf_frontier_records(a, each_side=6)
    if records or orbitals.get("homo_ev") is not None:
        story += [Paragraph("Frontier orbital energies", heading)]
        rows = [["Quantity", "Energy (eV)"]]
        for key, name in (("homo_ev", "HOMO"), ("lumo_ev", "LUMO"), ("gap_ev", "HOMO–LUMO gap"),
                          ("alpha_homo_ev", "Alpha HOMO"), ("alpha_lumo_ev", "Alpha LUMO"), ("beta_homo_ev", "Beta HOMO"), ("beta_lumo_ev", "Beta LUMO")):
            if _rf_num(orbitals.get(key)) is not None:
                rows.append([name, _rf_fmt(orbitals[key], 6)])
        if len(rows) > 1:
            story.append(table(rows))
        story.append(Paragraph("Orbital-energy gaps and frontier-orbital reactivity estimates require a converged wavefunction and are distinct from optical excitation energies. Spin channels are retained separately where valid rows are available.", small))
        descriptors = a.get("conceptual_dft", {}) or {}
        if descriptors:
            rows = [["Frontier-orbital estimate", "Value", "Unit"]]
            for key, name in (("ionization_potential_ev", "Ionization potential"), ("electron_affinity_ev", "Electron affinity"),
                              ("chemical_hardness_ev", "Chemical hardness"), ("chemical_potential_ev", "Chemical potential"),
                              ("electronegativity_ev", "Electronegativity"), ("chemical_softness_ev", "Chemical softness"),
                              ("electrophilicity_index_ev", "Electrophilicity index"), ("electrodonating_power_ev", "Electrodonating power"),
                              ("electroaccepting_power_ev", "Electroaccepting power"), ("net_electrophilicity_ev", "Net electrophilicity")):
                if _rf_num(descriptors.get(key)) is not None:
                    rows.append([name, _rf_fmt(descriptors[key], 6), "eV−1" if "softness" in key else "eV"])
            if len(rows) > 1:
                story += [Spacer(1, 6), table(rows, [8.5*cm, 5.0*cm, 3.5*cm])]
    dipole = a.get("dipole", {}) or {}
    charges = a.get("atomic_charges", []) or []
    geometry = a.get("final_geometry", []) or []
    if dipole or charges or geometry:
        story += [Paragraph("Molecular properties", heading)]
        rows = [["Property", "Value"]]
        if isinstance(dipole, dict) and _rf_num(dipole.get("magnitude_debye")) is not None:
            rows.append(["Dipole magnitude (debye)", _rf_fmt(dipole["magnitude_debye"], 6)])
        if charges:
            rows.append(["Atomic charge scheme", a.get("atomic_charge_type") or "Unverified"])
        if geometry:
            rows += [["Atoms in parsed geometry", str(len(geometry))], ["Geometry units", a.get("geometry_units") or "Unverified"]]
        story.append(table(rows))
    if plots:
        story += [Paragraph("Figures and export conventions", heading)]
        config = _rf_config(a)
        story.append(Paragraph("Each figure is supplied as a 600 dpi PNG, an editable SVG and a vector PDF. Numerical data and plot settings accompany the figures. Gaussian profiles preserve integrated stick strength; finite plot ranges and captured area are recorded in plot-settings JSON. σ is the standard deviation and FWHM = 2.354820 σ.", small))
        labels = {"optimization": "Optimization energy profile", "ir": "Calculated IR intensity density", "raman": "Calculated Raman activity density",
                  "uvvis": "Electronic absorption, wavelength density", "uvvis_energy": "Electronic absorption, energy density", "orbitals": "Frontier orbital energies"}
        for i, (key, path) in enumerate(plots.items(), 1):
            if not path or not os.path.isfile(path):
                continue
            image = Image(path)
            factor = min(17.0*cm/image.imageWidth, 9.0*cm/image.imageHeight)
            image.drawWidth = image.imageWidth * factor
            image.drawHeight = image.imageHeight * factor
            caption = Paragraph(f"Figure {i}. " + esc(labels.get(key, key)), small)
            story.append(KeepTogether([Spacer(1, 8), image, Spacer(1, 3), caption, Spacer(1, 7)]))
    for name, records_data in (("Atomic charges", charges), ("Cartesian geometry", geometry)):
        if not records_data:
            continue
        section_items = [Paragraph(name + " - numerical extract", heading)]
        if name == "Atomic charges":
            rows = [["Index", "Element", "Charge"]] + [[row.get("index", ""), row.get("element", ""), _rf_fmt(row.get("charge"), 8)] for row in records_data[:200] if isinstance(row, dict)]
            section_items.append(table(rows, [3*cm, 4*cm, 10*cm]))
        else:
            unit = str(a.get("geometry_units") or "unverified units")
            rows = [["Element", "X ("+unit+")", "Y ("+unit+")", "Z ("+unit+")"]] + [[row.get("element", ""), _rf_fmt(row.get("x"), 7), _rf_fmt(row.get("y"), 7), _rf_fmt(row.get("z"), 7)] for row in records_data[:200] if isinstance(row, dict)]
            section_items.append(table(rows, [2.5*cm, 4.83*cm, 4.83*cm, 4.84*cm]))
        if len(records_data) <= 12:
            story.append(KeepTogether(section_items))
        else:
            story.extend(section_items)
        if len(records_data) > 200:
            story.append(Paragraph("The report shows 200 entries; complete data are preserved in JSON and CSV.", small))
    pdf_path = os.path.abspath(str(pdf_path))
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    document = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=1.5*cm, bottomMargin=1.5*cm,
                                 title="Computational chemistry report", author="ChemBot", pageCompression=1)
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(doc.leftMargin, 0.85*cm, "ChemBot | scientific report " + REPORT_GENERATOR_VERSION)
        canvas.drawRightString(A4[0]-doc.rightMargin, 0.85*cm, str(doc.page))
        canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return pdf_path


def _rf_sha256(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _rf_data_csvs(a, outdir):
    paths = {}
    frequencies = [(i, _rf_num(value)) for i, value in enumerate(a.get("frequencies_cm1", []) or [])]
    frequencies = [(i, value) for i, value in frequencies if value is not None]
    if frequencies:
        paths["frequencies"] = _rf_csv_write(os.path.join(outdir, "frequencies.csv"), ["parsed_index", "frequency_cm1_unscaled"], frequencies)
    for name, fields in (("final_geometry", ["element", "x", "y", "z"]), ("atomic_charges", ["index", "element", "charge"])):
        rows = [row for row in a.get(name, []) or [] if isinstance(row, dict)]
        if rows:
            header = ["element", "x", "y", "z", "coordinate_units"] if name == "final_geometry" else fields + ["charge_scheme"]
            values = [[_rf_num(row.get(f)) if f in ("x", "y", "z", "charge") else row.get(f) for f in fields] + [a.get("geometry_units") if name == "final_geometry" else a.get("atomic_charge_type")] for row in rows]
            paths[name] = _rf_csv_write(os.path.join(outdir, name + ".csv"), header, values)
    orbitals = (a.get("orbitals", {}) or {}).get("orbitals", [])
    orbitals = [row for row in orbitals or [] if isinstance(row, dict) and not row.get("_synthetic")]
    if orbitals:
        fields = ["index", "spin", "occ", "eh", "ev"]
        paths["orbitals_all"] = _rf_csv_write(os.path.join(outdir, "orbitals_all.csv"), ["parsed_mo_index", "spin", "occupation", "energy_hartree", "energy_ev"],
                                             [[row.get(f) if f in ("index", "spin") else _rf_num(row.get(f)) for f in fields] for row in orbitals])
    return paths


def generate_report_bundle(a, outdir, base_name=None):
    """Build portable scientific exports; every returned file path is absolute."""
    outdir = os.path.abspath(str(outdir))
    os.makedirs(outdir, exist_ok=True)
    omitted = []
    analysis = _rf_json_safe(a, omitted)
    if not isinstance(analysis, dict):
        raise TypeError("Report analysis must be a dictionary.")
    warnings = _rf_warnings(analysis)
    if omitted:
        warnings.append(f"Nonfinite numbers in {len(omitted)} fields were exported as null; see export_nonfinite_fields.")
    analysis["warnings"] = warnings
    analysis["export_nonfinite_fields"] = omitted
    analysis["report_generator_version"] = REPORT_GENERATOR_VERSION
    analysis["plot_config"] = _rf_config(analysis)
    import numpy
    import matplotlib
    import reportlab
    software = {"python": sys.version.split()[0], "numpy": numpy.__version__, "matplotlib": matplotlib.__version__, "reportlab": reportlab.Version}
    generated_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    analysis["report_provenance"] = {"software_versions": software, "generated_utc": generated_utc,
                                     "report_generator_version": REPORT_GENERATOR_VERSION,
                                     "source_sha256": analysis.get("source_sha256") or (analysis.get("provenance") or {}).get("source_sha256")}
    plots = make_plots(analysis, outdir)
    numerical_csvs = _rf_data_csvs(analysis, outdir)
    figure_metadata = {}
    for key, path in plots.items():
        settings_path = str(Path(path).with_suffix("")) + "_plot_settings.json"
        if os.path.isfile(settings_path):
            with open(settings_path, encoding="utf-8") as handle:
                figure_metadata[key] = json.load(handle)
    analysis["plot_metadata"] = figure_metadata
    base = _rf_safe_base(base_name or analysis.get("filename") or "calculation")
    pdf_path = os.path.join(outdir, base + "_analysis_report.pdf")
    build_pdf(analysis, plots, pdf_path)
    analysis_path = _rf_json_write(os.path.join(outdir, "analysis.json"), analysis)
    # Only files generated for this bundle are included, even if callers reuse
    # an output directory containing unrelated or older report artifacts.
    candidates = set(numerical_csvs.values())
    for path in plots.values():
        figure_base = str(Path(path).with_suffix(""))
        candidates.update(figure_base + suffix for suffix in (".png", ".svg", ".pdf", "_plot_settings.json", "_sticks.csv", "_curve.csv", ".csv"))
    if "uvvis" in plots or "uvvis_energy" in plots:
        candidates.update(os.path.join(outdir, name) for name in ("uvvis_transitions.csv", "uvvis_energy_curve.csv", "uvvis_wavelength_curve.csv"))
    publication_files = sorted(os.path.abspath(path) for path in candidates if os.path.isfile(path))
    csv_files = {Path(path).stem: path for path in publication_files if path.endswith(".csv")}
    csv_files.update(numerical_csvs)
    archive_path = None
    if publication_files:
        archive_path = os.path.join(outdir, base + "_publication_figures.zip")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in publication_files:
                archive.write(path, arcname=os.path.basename(path))
    manifest = {"report_generator_version": REPORT_GENERATOR_VERSION, "filename": analysis.get("filename"), "engine": analysis.get("engine"),
                "source_sha256": analysis.get("source_sha256") or (analysis.get("provenance") or {}).get("source_sha256"),
                "source_hash_scope": analysis.get("source_hash_scope"), "source_bytes_sha256": analysis.get("source_bytes_sha256"),
                "parser_version": analysis.get("parser_version"), "analysis_schema_version": analysis.get("analysis_schema_version"),
                "generated_utc": generated_utc, "software_versions": software,
                "quality": analysis.get("quality", {}), "warnings": warnings, "units": analysis.get("units", {}),
                "geometry_units": analysis.get("geometry_units"), "atomic_charge_scheme": analysis.get("atomic_charge_type"),
                "plot_config": analysis["plot_config"], "plot_metadata": figure_metadata, "pdf": os.path.basename(pdf_path),
                "pdf_sha256": _rf_sha256(pdf_path), "analysis_json": os.path.basename(analysis_path), "analysis_sha256": _rf_sha256(analysis_path),
                "publication_files": [{"filename": os.path.basename(path), "sha256": _rf_sha256(path), "bytes": os.path.getsize(path)} for path in publication_files],
                "figures_zip": os.path.basename(archive_path) if archive_path else None,
                "synthetic_validation_example": _rf_synthetic(analysis)}
    manifest_path = _rf_json_write(os.path.join(outdir, "report_manifest.json"), manifest)
    return {"plots": plots, "pdf": pdf_path, "analysis_json": analysis_path, "manifest": manifest_path,
            "publication_files": publication_files, "csv_files": csv_files, "csv_paths": csv_files,
            "figure_metadata_paths": [str(Path(path).with_suffix("")) + "_plot_settings.json" for path in plots.values()],
            "figures_zip": archive_path, "version": REPORT_GENERATOR_VERSION}
'''

KAGGLE_RUNNER_CODE = r'''


import os, sys, json, time, base64, shutil, zipfile, tarfile, glob, subprocess, traceback, urllib.request, urllib.parse, hashlib, re, signal, threading, shlex, stat
from pathlib import Path
import uuid
START_TIME=time.time()
HARD_LIMIT=11.5*3600
SAFETY_MARGIN=20*60
WORK_DIR='/kaggle/working'
MAX_DISK_MB=20000
MAX_DISK_BYTES=MAX_DISK_MB*1000*1000

def tg(method, fields=None, file_field=None, file_path=None, timeout=90):
    fields = fields or {}
    url = 'https://api.telegram.org/bot' + BOT_TOKEN + '/' + method
    timeout = _phase_timeout(timeout)
    if file_field and file_path:
        import requests
        with open(file_path, 'rb') as handle:
            response = requests.post(url, data=fields, files={file_field: handle}, timeout=timeout)
        try:
            payload = response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            raise RuntimeError('Telegram returned a response that could not be validated.')
        if not payload.get('ok', False):
            error = RuntimeError('Telegram API error: ' + _redact_message(payload.get('description', 'unspecified error')))
            error.retry_after = (payload.get('parameters') or {}).get('retry_after')
            raise error
        response.raise_for_status()
        return payload
    data = urllib.parse.urlencode(fields).encode('utf-8')
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout).read()
    except Exception as exc:
        raise RuntimeError('Telegram request failed: ' + _redact_message(exc)) from exc
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError('Telegram returned a response that could not be validated.')
    if not isinstance(payload, dict) or not payload.get('ok', False):
        raise RuntimeError('Telegram API error: ' + _redact_message(payload.get('description') if isinstance(payload, dict) else 'invalid payload'))
    return payload

def send_msg(msg):
    for attempt in range(3):
        try:
            prefix = '[' + str(_JOB_STATUS.get('job_id') or 'ChemBot') + '] '
            tg('sendMessage', {'chat_id': CHAT_ID, 'text': (prefix + _redact_message(msg))[:3900]})
            return True
        except Exception:
            if attempt < 2:
                try:
                    _sleep_with_budget(2 ** attempt)
                except TimeoutError:
                    break
    print('[Kaggle] Telegram message could not be delivered.', file=sys.stderr)
    return False

def safe_install(cmd, max_seconds=2400):
    send_msg('[Kaggle] Installing: ' + ' '.join(map(str, cmd)))
    log_path = os.path.join('/tmp', 'chembot_setup_' + uuid.uuid4().hex + '.log')
    try:
        timeout = _phase_timeout(max_seconds, reserve_seconds=SAFETY_MARGIN)
        with open(log_path, 'wb') as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, TimeoutError) as exc:
                _stop_process_group(proc)
                proc.wait(timeout=5)
                raise TimeoutError('Dependency installation exceeded its bounded time budget.') from exc
        if rc != 0:
            with open(log_path, 'rb') as log:
                log.seek(max(0, os.path.getsize(log_path) - 3000))
                excerpt = log.read().decode('utf-8', errors='replace')
            raise RuntimeError('Dependency installation failed: ' + ' '.join(map(str, cmd)) + '\n' + _redact_message(excerpt))
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass

def remaining_time():
    return _phase_timeout(reserve_seconds=SAFETY_MARGIN)


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


def _find_gaussian_archive(input_root='/kaggle/input'):
    """Find the Gaussian dataset file, including the common tbz/tzb typo.

    Kaggle mounts the dataset under its slug; the archive's basename is
    retained. A .tzb upload may still be a valid bzip2-compressed tarball.
    """
    matches=[]; visible=[]
    if not os.path.isdir(input_root):
        raise RuntimeError('Kaggle input directory is unavailable: '+input_root)
    accepted={'g16.tbz','g16.tzb','g16.tbz2','g16.tar.bz2'}
    for parent, dirs, files in os.walk(input_root):
        dirs.sort()
        for name in sorted(files):
            path=os.path.join(parent,name)
            relative=os.path.relpath(path,input_root)
            if len(visible)<25: visible.append(relative)
            if name.lower() in accepted:
                in_dataset='gauusian16' in Path(relative).parts[0].lower()
                matches.append((not in_dataset,relative.lower(),path))
    if not matches:
        found=', '.join(visible) if visible else '(no files visible)'
        raise RuntimeError('No Gaussian g16 archive found under '+input_root+
                           '. Expected g16.tzb or g16.tbz in the gauusian16 dataset. '+
                           'Files visible: '+found)
    matches.sort()
    return matches[0][2]


def _ensure_gaussian_csh():
    """Gaussian's UNIX installer and companion scripts require C shell."""
    shell = shutil.which('csh')
    if shell:
        return shell
    apt = shutil.which('apt-get')
    if not apt or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Gaussian requires csh, which is missing. Automatic setup needs apt-get and root access; install tcsh so that /bin/csh is available.')
    print('[Kaggle] Gaussian: installing the missing C shell (tcsh).', flush=True)
    apt_options = ['-o', 'Acquire::Retries=1', '-o', 'Acquire::http::Timeout=30',
                   '-o', 'Acquire::https::Timeout=30']
    try:
        safe_install([apt] + apt_options + ['update', '-qq'], max_seconds=300)
        safe_install([apt] + apt_options + ['install', '-y', '--no-install-recommends', 'tcsh'], max_seconds=300)
    except Exception as exc:
        raise RuntimeError('Could not install the C shell required by Gaussian. Check Kaggle Internet access and the package-manager error: ' + _redact_message(exc)) from None
    shell = shutil.which('csh')
    if not shell:
        raise RuntimeError('tcsh installation completed, but csh is still unavailable. Gaussian needs a working /bin/csh interpreter.')
    return shell


def _prepare_gaussian_runtime_files(gaussian_home):
    """Repair only the extracted runtime, preserving binary bytes and links."""
    normalized = 0
    for parent, dirs, files in os.walk(gaussian_home, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(parent, d))]
        os.chmod(parent, (stat.S_IMODE(os.stat(parent).st_mode) & 0o770) | 0o700)
        for name in files:
            path = os.path.join(parent, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            mode = (stat.S_IMODE(os.stat(path).st_mode) & 0o770) | 0o600
            with open(path, 'rb') as handle:
                header = handle.read(4096)
            text_header = header.removeprefix(b'\xef\xbb\xbf')
            shell_file = text_header.startswith(b'#!')
            known_script = (os.path.basename(parent) == 'bsd' and
                            name in {'install', 'g16.profile', 'g16.login'})
            if header.startswith(b'\x7fELF') or shell_file or name in {'g16', 'formchk'}:
                mode |= 0o110
            os.chmod(path, mode)
            if (shell_file or known_script) and os.path.getsize(path) <= 2 * 1024 * 1024:
                raw = Path(path).read_bytes()
                if b'\x00' not in raw:
                    clean = raw.removeprefix(b'\xef\xbb\xbf').replace(b'\r\n', b'\n')
                    if clean != raw:
                        Path(path).write_bytes(clean)
                        normalized += 1
    if normalized:
        print('[Kaggle] Gaussian: normalized Linux script line endings in %d runtime files.' % normalized, flush=True)
    return normalized


def _gaussian_install_command(install_script):
    """Honor the installer shell instead of relying on the kernel shebang."""
    with open(install_script, 'rb') as handle:
        header = handle.read(4096)
    if header.startswith(b'\x7fELF'):
        return [install_script]
    if b'\x00' in header:
        raise RuntimeError('Gaussian bsd/install is neither a Linux ELF executable nor a text script. Check the Linux archive.')
    first = header.removeprefix(b'\xef\xbb\xbf').split(b'\n', 1)[0].rstrip(b'\r')
    if not first.startswith(b'#!'):
        # The documented UNIX installation procedure runs in C shell.
        return [_ensure_gaussian_csh(), '-f', install_script]
    try:
        parts = shlex.split(first[2:].decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('Invalid interpreter line in Gaussian bsd/install: ' + str(exc)) from None
    if parts and os.path.basename(parts[0]) == 'env':
        parts = parts[1:]
        if parts and parts[0] == '-S':
            parts = parts[1:]
    if not parts or parts[0].startswith('-'):
        raise RuntimeError('Unrecognized interpreter line in Gaussian bsd/install.')
    requested, arguments = parts[0], parts[1:]
    name = os.path.basename(requested)
    interpreter = shutil.which(requested)
    if not interpreter:
        interpreter = shutil.which(name)
    if not interpreter and name in {'csh', 'tcsh'}:
        interpreter = _ensure_gaussian_csh()
    if not interpreter:
        raise RuntimeError('Gaussian bsd/install exists, but its interpreter is missing: ' + requested)
    print('[Kaggle] Gaussian installer interpreter: ' + interpreter, flush=True)
    return [interpreter] + arguments + [install_script]


def _run_gaussian_installer(install_script, gaussian_home, gaussian_env):
    if not os.path.isfile(install_script):
        if os.path.lexists(install_script):
            raise RuntimeError('Gaussian bsd/install is a broken link or is not a regular file: ' + install_script)
        print('[Kaggle] Gaussian: bsd/install is absent; using the supplied installed runtime.', flush=True)
        return None
    command = _gaussian_install_command(install_script)
    log_path = os.path.join(WORK_DIR, 'outputs', 'gaussian_setup.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, 'wb') as log:
            log.write(('Installer: ' + install_script + '\nInterpreter: ' + command[0] + '\n').encode('utf-8'))
            log.flush()
            proc = subprocess.Popen(command, cwd=gaussian_home, env=gaussian_env,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = proc.wait(timeout=_phase_timeout(300, reserve_seconds=SAFETY_MARGIN))
            except (subprocess.TimeoutExpired, TimeoutError):
                _stop_process_group(proc)
                proc.wait(timeout=5)
                raise TimeoutError('Gaussian bsd/install exceeded its time budget. See outputs/gaussian_setup.log.') from None
    except FileNotFoundError:
        raise RuntimeError('Gaussian bsd/install exists but could not start. Its interpreter or Linux executable loader is unavailable. See outputs/gaussian_setup.log.') from None
    if rc != 0:
        with open(log_path, 'rb') as log:
            log.seek(max(0, os.path.getsize(log_path) - 2500))
            excerpt = log.read().decode('utf-8', errors='replace')
        raise RuntimeError('Gaussian bsd/install failed (exit %d):\n%s\nSee outputs/gaussian_setup.log.' % (rc, _redact_message(excerpt)))
    return log_path


def _cap_gaussian_maxdisk(inp):
    """Apply Gaussian's route keyword to every Link1 job, including GaussView inputs."""
    lines=inp.splitlines(keepends=True)
    starts=[i for i,line in enumerate(lines) if re.match(r'^\s*#',line)]
    if not starts:
        raise RuntimeError('Gaussian input has no route line beginning with #.')
    disk_option=re.compile(r'(?i)\bMaxDisk\s*=\s*(?P<number>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?B|[KMG]W)?\b')
    # Use conservative binary conversions for larger units. For example,
    # 20 GB may mean 20,480 MB and must therefore be capped at 20,000 MB.
    unit_mb={'B':1/(1024*1024),'KB':1/1024,'MB':1.0,'GB':1024.0,'TB':1048576.0,
             'KW':8/1024,'MW':8.0,'GW':8192.0,'':8/(1024*1024)}
    for start in reversed(starts):
        end=start+1
        while end<len(lines) and lines[end].strip():
            end+=1
        segment=lines[start:end]
        choices=[]
        for line in segment:
            for match in disk_option.finditer(line):
                amount=float(match.group('number'))*unit_mb[(match.group('unit') or '').upper()]
                if amount>0: choices.append((amount,match.group(0).strip()))
        # Preserve a smaller user-provided limit, but replace unlimited or
        # larger values. Exactly one option remains in each Link1 route.
        chosen=min(choices,key=lambda item:item[0]) if choices else None
        option=chosen[1] if chosen and chosen[0]<=MAX_DISK_MB else 'MaxDisk=20000MB'
        segment=[disk_option.sub('',line) for line in segment]
        segment=[line for line in segment if line.strip()]
        if not segment:
            raise RuntimeError('Gaussian route section is empty.')
        if not segment[-1].endswith('\n'):
            segment[-1]+='\n'
        segment.append(' '+option+'\n')
        lines[start:end]=segment
    return ''.join(lines)


def _cap_orca_maxdisk(inp):
    """Cap only the conventional-SCF disk option; retain other input tokens."""
    jobs = re.split(r'(?im)(^\s*\$new_job\s*$)', inp)
    for index in range(0, len(jobs), 2):
        job = jobs[index]
        scf = re.search(r'(?im)^\s*%scf\b', job)
        if scf is None:
            jobs[index] = '%scf\n  MaxDisk 20000\nend\n' + job
            continue
        # Preserve offsets while ignoring quoted filenames and comments.
        masked = re.sub(r'"[^"\n]*"|#[^\n]*', lambda m: ' ' * len(m.group()), job)
        end = re.search(r'(?i)\bend\b', masked[scf.end():])
        if end is None:
            raise RuntimeError('ORCA %scf block has no closing end keyword.')
        stop = scf.end() + end.start()
        content = job[scf.end():stop]
        pattern = r'(?i)\bMaxDisk\s*(?:=\s*)?([-+]?\d+(?:\.\d+)?)\b'
        visible = masked[scf.end():stop]
        choices = list(re.finditer(pattern, visible))
        if choices:
            for match in reversed(choices):
                number = float(match.group(1))
                if number <= 0 or number > MAX_DISK_MB:
                    content = content[:match.start(1)] + str(MAX_DISK_MB) + content[match.end(1):]
            jobs[index] = job[:scf.end()] + content + job[stop:]
        else:
            jobs[index] = job[:stop] + ' MaxDisk 20000\n' + job[stop:]
    return ''.join(jobs)


def _working_disk_bytes(root):
    total = 0
    seen = set()
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(parent, name))]
        for name in files:
            try:
                info = os.stat(os.path.join(parent, name), follow_symlinks=False)
                inode = (info.st_dev, info.st_ino)
                if inode not in seen:
                    seen.add(inode)
                    total += info.st_blocks * 512 if hasattr(info, 'st_blocks') else info.st_size
            except FileNotFoundError:
                continue
    return total


def _stop_process_group(proc):
    try: os.killpg(proc.pid,signal.SIGKILL)
    except (OSError,ProcessLookupError):
        if proc.poll() is None: proc.kill()


def _start_disk_guard(proc, root, extra_roots=()):
    stop=threading.Event(); exceeded=threading.Event()
    def watch():
        while proc.poll() is None and not stop.wait(2):
            roots = list(dict.fromkeys([os.path.realpath(root)] + [os.path.realpath(p) for p in extra_roots if p]))
            if (sum(_working_disk_bytes(p) for p in roots if os.path.isdir(p)) > MAX_DISK_BYTES
                    or shutil.disk_usage(root).free < 256 * 1024 * 1024):
                exceeded.set()
                _stop_process_group(proc)
                break
    guard=threading.Thread(target=watch,daemon=True)
    guard.start()
    return stop,exceeded,guard


# Official Telegram Bot API accepts documents up to 50 MB. 47 MiB is kept
# below 50,000,000 bytes and leaves a practical safety margin.
TG_SAFE_FILE_BYTES=47*1024*1024
TG_SEND_RETRIES=3
TG_SEND_DELAY=1.10


def _sha256(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            _phase_timeout(reserve_seconds=20)
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _send_document_retry(path, caption='', retries=TG_SEND_RETRIES, reserve_seconds=30):
    last = None
    for attempt in range(1, retries + 1):
        try:
            timeout = _phase_timeout(180, reserve_seconds=reserve_seconds)
            tg('sendDocument', {'chat_id': CHAT_ID, 'caption': _redact_message(caption)[:1024]},
               'document', path, timeout=timeout)
            try:
                _sleep_with_budget(TG_SEND_DELAY, reserve_seconds=reserve_seconds)
            except TimeoutError:
                pass
            return True
        except TimeoutError as exc:
            last = exc
            break
        except Exception as exc:
            last = exc
            if attempt < retries:
                delay = getattr(exc, 'retry_after', None) or min(2 ** attempt, 8)
                try:
                    _sleep_with_budget(float(delay), reserve_seconds=reserve_seconds)
                except TimeoutError:
                    break
    _record_error('telegram_transfer', last or RuntimeError('Unspecified delivery failure.'), os.path.basename(path))
    return False


def _iter_file_parts(path, chunk_bytes=None, part_prefix=None):
    """Yield one lossless part and remove it before creating the next."""
    chunk_bytes = TG_SAFE_FILE_BYTES if chunk_bytes is None else int(chunk_bytes)
    if chunk_bytes <= 0:
        raise ValueError('Part size must be positive.')
    total = os.path.getsize(path)
    count = (total + chunk_bytes - 1) // chunk_bytes
    prefix = _windows_safe_basename(part_prefix or os.path.basename(path))
    with tempfile.TemporaryDirectory(prefix='chembot_part_') as part_dir:
        with open(path, 'rb') as original:
            for index in range(1, count + 1):
                _phase_timeout(reserve_seconds=30)
                _require_space(part_dir, min(chunk_bytes, total - (index - 1) * chunk_bytes))
                part = os.path.join(part_dir, prefix + '.part%03d-of-%03d' % (index, count))
                try:
                    with open(part, 'wb') as handle:
                        remaining = chunk_bytes
                        while remaining:
                            _phase_timeout(reserve_seconds=30)
                            block = original.read(min(4 * 1024 * 1024, remaining))
                            if not block:
                                break
                            handle.write(block)
                            remaining -= len(block)
                    yield part, index, count
                finally:
                    try:
                        os.unlink(part)
                    except FileNotFoundError:
                        pass


def _write_transfer_manifest(results_dir, manifest_path):
    entries = _result_entries(results_dir)
    with open(manifest_path, 'w', encoding='utf-8') as handle:
        handle.write('ChemBot recursive result transfer manifest\n')
        handle.write('Scientific results, exact uploaded inputs, reports, figures and numeric data.\n\n')
        for entry in entries:
            handle.write('%s\t%d bytes\tSHA256=%s\n' % (
                entry['file'], entry['size_bytes'], entry.get('sha256') or 'UNAVAILABLE'))
            if entry.get('checksum_error'):
                handle.write('  Checksum error: ' + _redact_message(entry['checksum_error']) + '\n')
            if entry.get('part_prefix'):
                handle.write('  Part prefix: ' + entry['part_prefix'] + '\n')
        handle.write('\nLossless parts are numbered; reconstruct each original in binary numeric order.\n')
        handle.write('Linux/macOS: cat PREFIX.part* > original_filename\n')
        handle.write('Windows CMD: copy /b PREFIX.part001-of-NNN+PREFIX.part002-of-NNN+... original_filename\n')
        if _EXPORT_FAILURES:
            handle.write('\nPackaging failures (original files may remain in the Kaggle working directory):\n')
            for name in dict.fromkeys(_EXPORT_FAILURES):
                handle.write(_redact_message(name) + '\n')
    return entries


def send_results_with_fallback(archive, results_dir):
    failed = list(dict.fromkeys(_EXPORT_FAILURES))
    archive_size = os.path.getsize(archive) if archive and os.path.isfile(archive) else 0
    if archive_size and archive_size <= TG_SAFE_FILE_BYTES:
        if _send_document_retry(archive, 'Complete calculation and scientific report bundle'):
            return {'mode': 'archive', 'sent': 1, 'failed': failed}
        send_msg('[Transfer] Archive delivery failed; preserving and retrying the original files individually.')
    entries = _result_entries(results_dir, include_status=False)
    priority = {'.out': 0, '.log': 0, '.pdf': 1, '.json': 2, '.csv': 3, '.png': 4,
                '.svg': 5, '.fchk': 6, '.chk': 7, '.gbw': 8, '.cube': 20, '.dat': 21}
    def order(entry):
        name = entry['file'].lower()
        rank = min((value for suffix, value in priority.items() if name.endswith(suffix)), default=10)
        return rank, entry['size_bytes'], name
    sent = 0
    for entry in sorted(entries, key=order):
        path = os.path.join(results_dir, entry['file'])
        try:
            _phase_timeout(reserve_seconds=30)
            if entry['size_bytes'] <= TG_SAFE_FILE_BYTES:
                if _send_document_retry(path, 'Result: ' + entry['file']):
                    sent += 1
                else:
                    failed.append(entry['file'])
                continue
            iterator = _iter_file_parts(path, part_prefix=entry.get('part_prefix'))
            complete = True
            try:
                for part, index, count in iterator:
                    caption = '%s | part %d/%d | SHA256 %s' % (
                        entry['file'], index, count, str(entry.get('sha256') or 'unavailable'))
                    if _send_document_retry(part, caption):
                        sent += 1
                    else:
                        complete = False
            finally:
                iterator.close()
            if not complete:
                failed.append(entry['file'])
        except Exception as exc:
            failed.append(entry['file'])
            _record_error('result_transfer', exc, entry['file'])
    return {'mode': 'fallback', 'sent': sent, 'failed': list(dict.fromkeys(failed))}


import tempfile
import stat

_JOB_STATUS = {
    'schema_version': 1,
    'job_id': str(globals().get('JOB_ID') or str(globals().get('KAGGLE_NOTEBOOK_URL', '')).rstrip('/').rsplit('/', 1)[-1]),
    'engine': str(globals().get('JOB_ENGINE', 'unknown')),
    'input_file': str(globals().get('INPUT_FILE', '')),
    'notebook_url': str(globals().get('KAGGLE_NOTEBOOK_URL', '')),
    'calculation': {'state': 'not_started'},
    'analysis': {'state': 'not_started'},
    'transfer': {'state': 'not_started'},
    'errors': [],
}
_JOB_FINALIZED = False
_EXPORT_FAILURES = []


def _redact_message(value):
    text = str(value or '')
    values = [globals().get('BOT_TOKEN'), globals().get('DRIVE_LINK')]
    values += [os.environ.get(name) for name in ('CHEMBOT_BOT_TOKEN', 'KAGGLE_API_TOKEN', 'KAGGLE_KEY')]
    for secret in values:
        if secret and len(str(secret)) >= 8:
            text = text.replace(str(secret), '[REDACTED]')
    text = re.sub(r'https://api\.telegram\.org/bot[^/\s]+', 'https://api.telegram.org/bot[REDACTED]', text)
    text = re.sub(r'\b\d{6,16}:[A-Za-z0-9_-]{20,}\b', '[REDACTED_BOT_TOKEN]', text)
    return text


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not __import__('math').isfinite(value):
        return None
    if isinstance(value, str):
        return _redact_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_message(value)


def _status_path():
    return os.path.join(WORK_DIR, 'outputs', 'job_status.json')


def _write_status(phase=None, **updates):
    if phase is not None:
        _JOB_STATUS['phase'] = phase
    _JOB_STATUS.update(updates)
    _JOB_STATUS['updated_at_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    _JOB_STATUS['elapsed_seconds'] = round(max(0.0, time.time() - START_TIME), 3)
    path = _status_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(_json_safe(_JOB_STATUS), handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    except Exception as exc:
        print('[Status] Could not persist job status: ' + _redact_message(exc), file=sys.stderr)
    return path


def _record_error(stage, exc, filename=None):
    item = {'stage': stage, 'type': type(exc).__name__, 'message': _redact_message(exc)[:2000]}
    if filename:
        item['file'] = _redact_message(filename)
    _JOB_STATUS.setdefault('errors', []).append(item)
    _write_status()
    return item


def _phase_timeout(max_seconds=None, reserve_seconds=0):
    available = HARD_LIMIT - (time.time() - START_TIME) - float(reserve_seconds)
    if available <= 0:
        raise TimeoutError('The job time budget is exhausted; available artifacts are being preserved.')
    return available if max_seconds is None else min(float(max_seconds), available)


def _sleep_with_budget(seconds, reserve_seconds=0):
    time.sleep(min(float(seconds), _phase_timeout(reserve_seconds=reserve_seconds)))


def _persisted_bytes(root):
    total = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(parent, name))]
        for name in files:
            path = os.path.join(parent, name)
            if os.path.islink(path):
                continue
            try:
                total += os.path.getsize(path)
            except FileNotFoundError:
                continue
    return total


def _require_space(destination, additional_bytes=0):
    os.makedirs(destination, exist_ok=True)
    reserve = 32 * 1024 * 1024
    if shutil.disk_usage(destination).free < int(additional_bytes) + reserve:
        raise OSError('Insufficient free disk space to preserve/export the next artifact.')


def _stage_artifact(source, destination, remove_source=False):
    """Keep one canonical artifact; hard links avoid temporary copies on one filesystem."""
    source = os.path.abspath(source)
    destination = os.path.abspath(destination)
    if source == destination:
        return destination
    if os.path.islink(source) or not os.path.isfile(source):
        raise ValueError('Artifact is not a regular file: ' + os.path.basename(source))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.lexists(destination):
        if os.path.isfile(destination) and os.path.samefile(source, destination):
            if remove_source:
                os.unlink(source)
            return destination
        os.unlink(destination)
    try:
        os.link(source, destination)
    except OSError:
        _require_space(os.path.dirname(destination), os.path.getsize(source))
        partial = destination + '.partial'
        try:
            with open(source, 'rb') as original, open(partial, 'wb') as copied:
                while True:
                    _phase_timeout(reserve_seconds=60)
                    block = original.read(4 * 1024 * 1024)
                    if not block:
                        break
                    copied.write(block)
            if os.path.getsize(partial) != os.path.getsize(source):
                raise OSError('Artifact changed or the copy was incomplete.')
            shutil.copystat(source, partial)
            os.replace(partial, destination)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
    if remove_source:
        os.unlink(source)
    return destination


def _stage_uploaded_inputs():
    """Persist exact received bytes before installs or engine input normalization."""
    global INPUT_FILE
    files = json.loads(base64.b64decode(ENCODED_FILES_JSON).decode('utf-8'))
    seen = set()
    original_dir = os.path.join(WORK_DIR, 'outputs', 'inputs')
    os.makedirs(original_dir, exist_ok=True)
    index = []
    for original_name, encoded in files.items():
        safe_name = _windows_safe_basename(original_name)
        if safe_name in seen:
            raise RuntimeError('Two uploaded files map to the same Windows-safe filename: ' + safe_name)
        seen.add(safe_name)
        data = base64.b64decode(encoded, validate=True)
        _require_space(WORK_DIR, 2 * len(data))
        Path(WORK_DIR, safe_name).write_bytes(data)
        Path(original_dir, safe_name).write_bytes(data)
        index.append({'original_name': str(original_name), 'staged_name': safe_name,
                      'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    Path(original_dir, 'received_inputs.json').write_text(
        json.dumps(_json_safe(index), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    INPUT_FILE = _windows_safe_basename(INPUT_FILE)
    # Preserve the exact attachment before writing the normalized executable input.
    prepared = globals().get('PREPARED_INPUT_TEXT')
    if prepared is not None:
        if not isinstance(prepared, str) or '\x00' in prepared:
            raise RuntimeError('The prepared calculation input is not valid text.')
        Path(WORK_DIR, INPUT_FILE).write_text(prepared, encoding='utf-8', newline='\n')
    _write_status('preparing', input_file=INPUT_FILE, raw_inputs_preserved=True)


def _collect_calculation_files(results_dir):
    failures = []
    root = os.path.abspath(WORK_DIR)
    excluded = {os.path.join(root, name) for name in ('outputs', 'analysis_report', 'psi4_scratch', 'gaussian_scratch')}
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if os.path.join(parent, name) not in excluded
                   and not os.path.islink(os.path.join(parent, name))]
        for filename in sorted(files):
            source = os.path.join(parent, filename)
            if os.path.islink(source):
                continue
            # Kaggle runtime source and secret/config files are not scientific results.
            low = filename.lower()
            if low.endswith(('.py', '.ipynb', '.pyc')) or low in {'kaggle.json', 'access_token', '.env', 'restart_files.zip'}:
                continue
            relative = os.path.relpath(source, root)
            destination = os.path.join(results_dir, 'calculation', relative)
            try:
                _stage_artifact(source, destination, remove_source=True)
            except Exception as exc:
                failures.append(relative)
                _record_error('result_preservation', exc, relative)
    if failures:
        _EXPORT_FAILURES.extend(failures)
    _write_status(raw_results_preserved=not failures, preservation_failures=failures)
    return failures


def _bundle_file_paths(bundle):
    if not isinstance(bundle, dict):
        return []
    paths = []
    for key in ('pdf', 'analysis_json', 'manifest', 'figures_zip'):
        if bundle.get(key):
            paths.append(str(bundle[key]))
    paths.extend(str(path) for path in (bundle.get('publication_files') or []))
    for key in ('plots', 'csv_files', 'csv_paths'):
        value = bundle.get(key) or {}
        if isinstance(value, dict):
            paths.extend(str(path) for path in value.values() if path)
    return list(dict.fromkeys(paths))


def _stage_scientific_report(bundle, results_dir):
    """Include canonical bundle keys and partial report files if generation failed."""
    report_root = os.path.abspath(os.path.join(WORK_DIR, 'analysis_report'))
    paths = _bundle_file_paths(bundle)
    if os.path.isdir(report_root):
        for parent, dirs, files in os.walk(report_root, followlinks=False):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(parent, name))]
            paths.extend(os.path.join(parent, name) for name in files)
    for source in dict.fromkeys(map(os.path.abspath, paths)):
        if not os.path.isfile(source) or os.path.islink(source):
            continue
        if source.startswith(report_root + os.sep):
            relative = os.path.relpath(source, report_root)
        else:
            relative = hashlib.sha256(source.encode()).hexdigest()[:10] + '_' + _windows_safe_basename(source)
        destination = os.path.join(results_dir, 'analysis_report', relative)
        try:
            _stage_artifact(source, destination, remove_source=source.startswith(report_root + os.sep))
        except Exception as exc:
            _EXPORT_FAILURES.append('analysis_report/' + relative)
            _record_error('report_preservation', exc, relative)


def _restore_orca_executable_permissions(package_dir):
    restored = []
    for parent, dirs, files in os.walk(package_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(parent, name))]
        for filename in files:
            known = bool(re.fullmatch(r'orca(?:_[A-Za-z0-9_-]+)?', filename)) or filename in {
                'mpirun', 'mpiexec', 'orted', 'prted', 'ompi_info'}
            path = os.path.join(parent, filename)
            if not known or os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, 'rb') as handle:
                signature = handle.read(4)
            if not (signature.startswith(b'\x7fELF') or signature.startswith(b'#!')):
                continue
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & 0o111 != 0o111:
                    os.chmod(path, mode | 0o111)
                    restored.append(os.path.relpath(path, package_dir))
            except OSError as exc:
                raise RuntimeError('Could not restore executable permission for ' + filename) from exc
    return restored


def _result_entries(results_dir, include_status=True, with_checksums=True):
    entries = []
    for parent, dirs, files in os.walk(results_dir, followlinks=False):
        dirs[:] = [name for name in sorted(dirs) if not os.path.islink(os.path.join(parent, name))]
        for filename in sorted(files):
            if filename == 'CHEMBOT_TRANSFER_MANIFEST.txt' or (filename == 'job_status.json' and not include_status):
                continue
            path = os.path.join(parent, filename)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            relative = os.path.relpath(path, results_dir).replace(os.sep, '/')
            item = {'file': relative, 'size_bytes': os.path.getsize(path)}
            if with_checksums:
                try:
                    item['sha256'] = _sha256(path)
                except Exception as exc:
                    item['sha256'] = None
                    item['checksum_error'] = _redact_message(exc)
                    _record_error('checksum', exc, relative)
            if item['size_bytes'] > TG_SAFE_FILE_BYTES:
                item['part_prefix'] = hashlib.sha256(relative.encode()).hexdigest()[:10] + '_' + _windows_safe_basename(filename)
            entries.append(item)
    return entries


def _make_bounded_zip(results_dir, archive_stem):
    """Only build a small transfer ZIP, outside saved output; large raw files stream individually."""
    entries = _result_entries(results_dir, include_status=False, with_checksums=False)
    total = sum(item['size_bytes'] for item in entries)
    if total > TG_SAFE_FILE_BYTES - 256 * 1024:
        return ''
    _phase_timeout(reserve_seconds=90)
    folder = tempfile.mkdtemp(prefix='chembot_archive_')
    archive = os.path.join(folder, _windows_safe_basename(archive_stem) + '.zip')
    try:
        _require_space(folder, total + 1024 * 1024)
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=4) as handle:
            for entry in entries:
                _phase_timeout(reserve_seconds=60)
                handle.write(os.path.join(results_dir, entry['file']), arcname=entry['file'])
        return archive
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _deliver_final_metadata(results_dir, transfer):
    status_path = _write_status()
    manifest_path = os.path.join(results_dir, 'CHEMBOT_TRANSFER_MANIFEST.txt')
    _write_transfer_manifest(results_dir, manifest_path)
    missing = []
    for path, caption in ((status_path, 'Final calculation, analysis and delivery status'),
                          (manifest_path, 'Complete recursive transfer manifest / SHA-256')):
        if not _send_document_retry(path, caption, reserve_seconds=0):
            missing.append(os.path.basename(path))
    if missing:
        transfer.setdefault('failed', []).extend(missing)
        _JOB_STATUS['transfer'] = dict(transfer, state='partial')
        _write_status()
        _write_transfer_manifest(results_dir, manifest_path)
    return missing

GAUSSIAN_SCRATCH_DIR=None
try:
    os.makedirs(WORK_DIR, exist_ok=True)
    os.chdir(WORK_DIR)
    _write_status('preparing')
    _stage_uploaded_inputs()
    PY_DEPS='/tmp/chembot_python_packages'
    os.makedirs(PY_DEPS,exist_ok=True)
    os.environ['PIP_CACHE_DIR']='/tmp/chembot_pip_cache'
    safe_install([sys.executable,'-m','pip','install','-q','--target',PY_DEPS,
                  'requests==2.34.2','matplotlib==3.10.8','reportlab==4.4.9','numpy==2.3.5'])
    sys.path.insert(0,PY_DEPS)
    send_msg('[Kaggle] Session connected. Preparing calculation...')
    os.makedirs(WORK_DIR,exist_ok=True)
    os.chdir(WORK_DIR)

    # Inputs were staged and snapshotted before dependency preparation.

    if DRIVE_LINK:
        send_msg('[Kaggle] Downloading restart archive...')
        if urllib.parse.urlsplit(DRIVE_LINK).scheme.lower()!='https':
            raise RuntimeError('Restart archive URL must use HTTPS.')
        req=urllib.request.Request(DRIVE_LINK,headers={'User-Agent':'ChemBot/4.0'})
        with urllib.request.urlopen(req,timeout=_phase_timeout(120, reserve_seconds=SAFETY_MARGIN)) as r, open('restart_files.zip','wb') as f:
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
            expanded = sum(max(0,info.file_size) for info in infos)
            if expanded > MAX_DISK_BYTES:
                raise RuntimeError('Restart archive expands beyond the calculation disk budget.')
            _require_space(WORK_DIR, expanded)
            restart_root=os.path.abspath('.')
            for info in infos:
                target=os.path.abspath(os.path.join('.',info.filename))
                if not (target.startswith(restart_root+os.sep) or (target==restart_root and info.is_dir())):
                    raise RuntimeError('Unsafe ZIP path detected.')
                relative = os.path.relpath(target, restart_root)
                if relative.split(os.sep)[0] in {'outputs', 'analysis_report'}:
                    raise RuntimeError('Restart ZIP cannot overwrite report/status directories.')
                if not info.is_dir() and os.path.lexists(target):
                    raise RuntimeError('Restart ZIP would overwrite an uploaded or active file: ' + relative)
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
        gaussian_input=_cap_gaussian_maxdisk(gaussian_input)
        # GaussianView on Windows commonly writes absolute Link 0 paths (and
        # CRLF files). Persist the normalized portable input even when the user
        # already supplied %chk/%oldchk, so Gaussian never receives a Windows
        # drive path inside the Linux Kaggle runtime.
        Path(INPUT_FILE).write_text(gaussian_input,encoding='utf-8',newline='\n')
    if is_psi4:
        send_msg('[Kaggle] Preparing Psi4 environment...')

        PSI4_PREFIX='/tmp/chembot_psi4_env'
        os.environ['CONDA_PKGS_DIRS']='/tmp/chembot_conda_pkgs'
        os.makedirs(os.environ['CONDA_PKGS_DIRS'],exist_ok=True)

        def _valid_conda(path):
            if not path or not os.path.isfile(path):
                return False
            try:
                p=subprocess.run([path,'--version'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=_phase_timeout(20))
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
            installer='/tmp/Psi4conda-1.11-py312-Linux-x86_64.sh'
            send_msg('[Kaggle] Conda was not available; using the official Psi4 1.11 standalone installer...')
            req=urllib.request.Request(url,headers={'User-Agent':'ChemBot/5.6'})
            with urllib.request.urlopen(req,timeout=_phase_timeout(1800, reserve_seconds=SAFETY_MARGIN)) as r, open(installer,'wb') as f:
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

        psi4_exe=os.path.join(PSI4_PREFIX,'bin','psi4')
        if not os.path.isfile(psi4_exe):
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
        probe=subprocess.run([psi4_exe,'--version'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=_phase_timeout(60))
        if probe.returncode!=0:
            raise RuntimeError('Psi4 executable failed its version check:\n'+(probe.stdout or '')[-1500:])
        send_msg('[Kaggle] '+(probe.stdout or 'Psi4 ready').strip().splitlines()[0][:300])
        psi4_scratch=os.path.join('/tmp','chembot_psi4_scratch')
        os.makedirs(psi4_scratch,exist_ok=True)
        psi4_env=os.environ.copy()
        psi4_env['PSI_SCRATCH']=psi4_scratch
        cmd=[psi4_exe,'-i',INPUT_FILE,'-o',output_file]
    elif is_gaussian:
        send_msg('[Kaggle] Preparing Gaussian 16 from attached Dataset...')
        gaussian_archive=_find_gaussian_archive()
        # Keep the large Gaussian installation in temporary storage, as with
        # the ORCA package. Calculation inputs and deliverables stay in the
        # notebook working directory and are returned to the user.
        GAUSSIAN_RUNTIME_ROOT='/tmp/gaussian16_runtime'
        shutil.rmtree(GAUSSIAN_RUNTIME_ROOT,ignore_errors=True)
        os.makedirs(GAUSSIAN_RUNTIME_ROOT,exist_ok=True)

        def _extract_gaussian_tbz(archive_path,destination):
            # tarfile checks the compression header; the Kaggle dataset names
            # its bzip2 tarball g16.tzb rather than g16.tbz.
            with tarfile.open(archive_path,'r:*') as archive:
                members=archive.getmembers()
                if not members:
                    raise RuntimeError('The Gaussian archive is empty: '+os.path.basename(archive_path))
                unpacked=sum(max(0,int(m.size)) for m in members if m.isfile())
                if unpacked>20*1024*1024*1024:
                    raise RuntimeError('The expanded Gaussian archive exceeds the 20 GiB safety limit.')
                free_bytes=shutil.disk_usage(destination).free
                if unpacked+512*1024*1024>free_bytes:
                    raise RuntimeError('Not enough free space in /tmp for the Gaussian archive: '+
                                       'need approximately %.1f GiB, available %.1f GiB.' %
                                       ((unpacked+512*1024*1024)/1024**3,free_bytes/1024**3))
                root=os.path.realpath(destination)
                for member in members:
                    name=member.name.replace('\\','/')
                    normalized=os.path.normpath(name)
                    if not name or name.startswith('/') or normalized in ('.','..') or normalized.startswith('..'+os.sep):
                        raise RuntimeError('Unsafe path in Gaussian archive: '+member.name)
                    target=os.path.realpath(os.path.join(destination,name))
                    if target!=root and not target.startswith(root+os.sep):
                        raise RuntimeError('Unsafe path in Gaussian archive: '+member.name)
                    if member.issym() or member.islnk():
                        link=member.linkname.replace('\\','/')
                        link_base=os.path.dirname(name) if member.issym() else ''
                        link_target=os.path.realpath(os.path.join(root,link_base,link))
                        if os.path.isabs(link) or (link_target!=root and not link_target.startswith(root+os.sep)):
                            raise RuntimeError('Unsafe link in Gaussian archive: '+member.name)
                    if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                        raise RuntimeError('Unsupported special file in Gaussian archive: '+member.name)
                if hasattr(tarfile,'data_filter'):
                    archive.extractall(destination,filter='data')
                else:
                    archive.extractall(destination)

        send_msg('[Kaggle] Found '+os.path.basename(gaussian_archive)+
                 '; extracting Gaussian 16 under /tmp before starting the calculation...')
        try:
            _extract_gaussian_tbz(gaussian_archive,GAUSSIAN_RUNTIME_ROOT)
        except tarfile.TarError as exc:
            raise RuntimeError('Could not unpack '+os.path.basename(gaussian_archive)+
                               ' as a tar archive. Check that the Dataset file is a complete Linux Gaussian 16 archive.') from exc
        profile_candidates=[]
        for parent,dirs,files in os.walk(GAUSSIAN_RUNTIME_ROOT):
            if os.path.basename(parent)=='bsd' and 'g16.profile' in files:
                profile_candidates.append(os.path.join(parent,'g16.profile'))
        if not profile_candidates:
            raise RuntimeError(os.path.basename(gaussian_archive)+' was extracted, but g16/bsd/g16.profile is missing. Check that the Dataset contains the Linux binary archive.')
        profile_candidates.sort(key=lambda p:(p.count(os.sep),p))
        GAUSSIAN_PROFILE=profile_candidates[0]
        gaussian_bsd=os.path.dirname(GAUSSIAN_PROFILE)
        gaussian_home=os.path.dirname(gaussian_bsd)
        gaussian_root=os.path.dirname(gaussian_home)
        if not os.path.isfile(os.path.join(gaussian_home,'g16')):
            raise RuntimeError('Gaussian archive extracted, but the g16 executable is missing next to bsd/g16.profile.')
        send_msg('[Kaggle] Gaussian 16 archive extracted and executable found. Preparing the runtime...')
        GAUSSIAN_SCRATCH_DIR=os.path.join('/tmp','chembot_gaussian_scratch')
        os.makedirs(GAUSSIAN_SCRATCH_DIR,exist_ok=True)
        gaussian_env=os.environ.copy()
        gaussian_env['g16root']=gaussian_root
        gaussian_env['GAUSS_SCRDIR']=GAUSSIAN_SCRATCH_DIR
        gaussian_env['CHEMBOT_G16_PROFILE']=GAUSSIAN_PROFILE
        gaussian_env['CHEMBOT_GAUSS_SCRDIR']=GAUSSIAN_SCRATCH_DIR

        _prepare_gaussian_runtime_files(gaussian_home)
        _ensure_gaussian_csh()
        install_script=os.path.join(gaussian_bsd,'install')
        _run_gaussian_installer(install_script,gaussian_home,gaussian_env)
        gaussian_exe=os.path.join(gaussian_home,'g16')
        if not os.path.exists(gaussian_exe):
            raise RuntimeError('Gaussian installation did not produce g16 at '+gaussian_exe)
        try: os.chmod(gaussian_exe,0o750)
        except OSError: pass
        gaussian_env['CHEMBOT_G16_EXE']=gaussian_exe
        gaussian_formchk=os.path.join(gaussian_home,'formchk')
        gaussian_env['CHEMBOT_FORMCHK_EXE']=gaussian_formchk if os.path.isfile(gaussian_formchk) else ''
        if gaussian_env['CHEMBOT_FORMCHK_EXE']:
            try: os.chmod(gaussian_formchk,0o750)
            except OSError: pass
        # The official profile initializes Gaussian paths and runtime variables.
        GAUSSIAN_LAUNCH_SCRIPT='source "$CHEMBOT_G16_PROFILE" || exit 126; export GAUSS_SCRDIR="$CHEMBOT_GAUSS_SCRDIR"; if command -v g16 >/dev/null 2>&1; then exec g16; fi; [ -x "$CHEMBOT_G16_EXE" ] || exit 127; exec "$CHEMBOT_G16_EXE"'
        cmd=['bash','-c',GAUSSIAN_LAUNCH_SCRIPT]
        send_msg('[Kaggle] Gaussian 16 runtime prepared from '+os.path.basename(gaussian_archive))
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
                send_msg('[Kaggle] ORCA archive extraction failed: '+_redact_message(exc))
            return False

        # Keep the ORCA program and its adjacent shared libraries together in
        # /tmp, even when Kaggle mounts an already unpacked dataset directory.
        orca_source=_find_orca_under('/kaggle/input')
        orca_exe=None
        if orca_source:
            orca_package=os.path.join(ORCA_SCRATCH,'package')
            shutil.rmtree(orca_package,ignore_errors=True)
            shutil.copytree(os.path.dirname(orca_source),orca_package)
            orca_exe=os.path.join(orca_package,'orca')
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
        orca_dir=os.path.dirname(os.path.realpath(orca_exe))
        _restore_orca_executable_permissions(orca_dir)
        if not os.access(orca_exe, os.X_OK):
            raise RuntimeError('ORCA executable has no usable execute permission.')
        os.environ['PATH']=orca_dir+os.pathsep+os.environ.get('PATH','')
        os.environ['LD_LIBRARY_PATH']=orca_dir+os.pathsep+os.environ.get('LD_LIBRARY_PATH','')
        os.environ['OMPI_ALLOW_RUN_AS_ROOT']='1'
        os.environ['OMPI_ALLOW_RUN_AS_ROOT_CONFIRM']='1'
        os.environ['OMPI_MCA_btl_vader_single_copy_mechanism']='none'
        os.environ['OMPI_MCA_rmaps_base_oversubscribe']='1'
        os.environ['OMP_NUM_THREADS']='1'
        os.environ['MKL_NUM_THREADS']='1'
        send_msg('[Kaggle] ORCA executable: '+orca_exe)
        orca_input=Path(INPUT_FILE).read_text(encoding='utf-8',errors='replace')
        Path(INPUT_FILE).write_text(_cap_orca_maxdisk(orca_input),encoding='utf-8',newline='\n')
        cmd=[orca_exe,INPUT_FILE]

    engine_label='Gaussian 16' if is_gaussian else ('Psi4' if is_psi4 else 'ORCA')
    scratch_roots = ([psi4_scratch] if is_psi4 else ([GAUSSIAN_SCRATCH_DIR] if is_gaussian else []))
    if _working_disk_bytes(WORK_DIR)>MAX_DISK_BYTES:
        raise RuntimeError('The working directory already exceeds the 20,000 MB calculation disk limit.')
    send_msg(f"[Kaggle] Launching {engine_label} calculation in {WORK_DIR}: {INPUT_FILE}; MaxDisk limit {MAX_DISK_MB} MB")
    _write_status('calculating', calculation={'state': 'running', 'engine': engine_label})
    timeout_triggered=False
    disk_guard_stop=None; disk_guard_exceeded=None; disk_guard_thread=None
    calculation_timeout = remaining_time()  # Check budget before starting a process.
    try:
        if is_psi4:
            launch_log = basename + '_launcher.log'
            with open(launch_log, 'wb') as launcher:
                proc=subprocess.Popen(cmd,stdout=launcher,stderr=subprocess.STDOUT,
                                      cwd=WORK_DIR,env=psi4_env,start_new_session=True)
                disk_guard_stop,disk_guard_exceeded,disk_guard_thread=_start_disk_guard(proc,WORK_DIR,scratch_roots)
                send_msg('✅ '+engine_label+' calculation process started on Kaggle. The private job link was sent by the bot when Kaggle accepted the submission.')
                try:
                    rc = proc.wait(timeout=calculation_timeout)
                except subprocess.TimeoutExpired:
                    _stop_process_group(proc); proc.wait(); rc=124; timeout_triggered=True
                    send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
            if not os.path.exists(output_file):
                shutil.copyfile(launch_log, output_file)
        elif is_gaussian:
            with open(INPUT_FILE,'rb') as fin, open(output_file,'wb') as fout:
                proc=subprocess.Popen(cmd,stdin=fin,stdout=fout,stderr=subprocess.STDOUT,
                                      cwd=WORK_DIR,env=gaussian_env,start_new_session=True)
                disk_guard_stop,disk_guard_exceeded,disk_guard_thread=_start_disk_guard(proc,WORK_DIR,scratch_roots)
                send_msg('✅ '+engine_label+' calculation process started on Kaggle. The private job link was sent by the bot when Kaggle accepted the submission.')
                try:
                    rc=proc.wait(timeout=calculation_timeout)
                except subprocess.TimeoutExpired:
                    _stop_process_group(proc); proc.wait(); rc=124; timeout_triggered=True
                    send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
        else:
            with open(output_file,'w',encoding='utf-8',errors='replace') as fout:
                proc=subprocess.Popen(cmd,stdout=fout,stderr=subprocess.STDOUT,
                                      cwd=WORK_DIR,start_new_session=True)
                disk_guard_stop,disk_guard_exceeded,disk_guard_thread=_start_disk_guard(proc,WORK_DIR)
                send_msg('✅ '+engine_label+' calculation process started on Kaggle. The private job link was sent by the bot when Kaggle accepted the submission.')
                try:
                    rc=proc.wait(timeout=calculation_timeout)
                except subprocess.TimeoutExpired:
                    _stop_process_group(proc); proc.wait(); rc=124; timeout_triggered=True
                    send_msg('[Kaggle] Safety timeout reached; preserving available restart/results files.')
    except Exception as exc:
        if 'proc' in globals() and proc.poll() is None:
            _stop_process_group(proc)
            proc.wait(timeout=10)
        send_msg('[Kaggle] Could not start or wait for '+engine_label+' process: '+_redact_message(exc))
        raise
    finally:
        if disk_guard_stop is not None:
            disk_guard_stop.set()
            disk_guard_thread.join(timeout=3)
    disk_limit_exceeded=(disk_guard_exceeded is not None and disk_guard_exceeded.is_set()) or _working_disk_bytes(WORK_DIR)>MAX_DISK_BYTES
    if disk_limit_exceeded:
        send_msg('[Kaggle] The calculation reached the 20,000 MB working-disk limit. Available output files will still be returned.')

    calculation_cause = ('disk_limit' if disk_limit_exceeded else
                         ('timeout' if timeout_triggered else ('success' if rc == 0 else 'process_failed')))
    _write_status('calculated', calculation={'state': 'complete' if calculation_cause == 'success' else 'failed',
                  'returncode': rc, 'cause': calculation_cause})
    bundle=None
    analysis={}
    report_error=None
    if is_gaussian:
        _write_status('converting_checkpoints', analysis={'state': 'pending',
                      'reason': 'Portable checkpoint conversion precedes scientific analysis.'})
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
        formchk_script='source "$CHEMBOT_G16_PROFILE" || exit 126; export GAUSS_SCRDIR="$CHEMBOT_GAUSS_SCRDIR"; if command -v formchk >/dev/null 2>&1; then exec formchk "$1" "$2"; fi; [ -x "$CHEMBOT_FORMCHK_EXE" ] || exit 127; exec "$CHEMBOT_FORMCHK_EXE" "$1" "$2"'
        for chk_path in sorted(set(checkpoints)):
            fchk_path=os.path.splitext(chk_path)[0]+'.fchk'
            try:
                conversion=subprocess.run(['bash','-c',formchk_script,'formchk',chk_path,fchk_path],env=gaussian_env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=_phase_timeout(1800, reserve_seconds=90))
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

    # Gaussian checkpoint conversion above is followed by the same validated
    # analysis/report path when the shared analyzer is injected for that engine.
    if callable(globals().get('parse_output_text')):
        _write_status('analyzing', analysis={'state': 'running'})
        try:
            _phase_timeout(reserve_seconds=120)
            if os.path.exists(output_file) and os.path.getsize(output_file) > 128 * 1024 * 1024:
                raise RuntimeError('Text output exceeds the 128 MiB analysis budget. Complete raw results are preserved without truncating scientific records.')
            out_bytes = Path(output_file).read_bytes() if os.path.exists(output_file) else b''
            out_text = out_bytes.decode('utf-8', errors='replace')
            analysis = parse_output_text(out_text, output_file)
            analysis['source_bytes_sha256'] = hashlib.sha256(out_bytes).hexdigest()
            analysis['process_returncode'] = rc
            analysis['process_return_cause'] = calculation_cause
            if callable(globals().get('validate_analysis')):
                analysis = validate_analysis(analysis, source_text=out_text)
            quality = analysis.get('quality') or analysis.get('validation') or {}
            _write_status('analyzing', analysis={'state': 'validated',
                          'quality_status': quality.get('status', 'UNVERIFIED'),
                          'normal_termination': bool(analysis.get('normal_termination')),
                          'quality': quality})
            work = 'analysis_report'
            os.makedirs(work, exist_ok=True)
            bundle = generate_report_bundle(analysis, work, basename)
            _write_status('analyzed', analysis={'state': 'complete',
                          'quality_status': quality.get('status', 'UNVERIFIED'),
                          'normal_termination': bool(analysis.get('normal_termination')),
                          'quality': quality})
            try:
                send_msg('[Analysis]\n' + section_summary(analysis))
            except Exception as exc:
                _record_error('analysis_notification', exc)
            # Preview only PNGs. Vector figures, numeric tables, plot settings and
            # source data remain in the complete publication/report bundle.
            for name, path in (bundle.get('plots') or {}).items():
                try:
                    tg('sendPhoto', {'chat_id': CHAT_ID, 'caption': str(name).replace('_', ' ').title()}, 'photo', path)
                except Exception as exc:
                    _record_error('preview_delivery', exc, str(name))
        except Exception as exc:
            report_error = exc
            _record_error('scientific_analysis', exc)
            _write_status('report_failed', analysis={'state': 'failed', 'error_type': type(exc).__name__,
                          'quality_status': (analysis.get('quality') or {}).get('status', 'UNVERIFIED'),
                          'quality': analysis.get('quality') or {}})
            send_msg('[Analysis] The scientific report could not be completed. Raw calculation files are being returned.')

    results_dir = os.path.join(WORK_DIR, 'outputs')
    os.makedirs(results_dir, exist_ok=True)
    _write_status('preserving')
    _collect_calculation_files(results_dir)
    _stage_scientific_report(bundle, results_dir)
    saved_size = _persisted_bytes(WORK_DIR)
    if saved_size > MAX_DISK_BYTES:
        error = RuntimeError('Saved output exceeds the configured 20,000 MB budget; direct delivery will be attempted.')
        _record_error('output_quota', error)
        _EXPORT_FAILURES.append('saved-output-quota')
    result_prefix = 'Gaussian16_Results_' if is_gaussian else ('Psi4_Results_' if is_psi4 else 'ORCA_Results_')
    archive = ''
    try:
        archive = _make_bounded_zip(results_dir, result_prefix + basename)
    except Exception as exc:
        _record_error('archive_creation', exc)
    _write_status('transferring', transfer={'state': 'running'})
    try:
        transfer = send_results_with_fallback(archive, results_dir)
    finally:
        if archive:
            shutil.rmtree(os.path.dirname(archive), ignore_errors=True)
    if calculation_cause != 'success':
        final_state = calculation_cause.upper()
    elif report_error is not None:
        final_state = 'REPORT_FAILED'
    elif not analysis:
        final_state = 'RAW_RESULTS_COMPLETE'
    elif not analysis.get('normal_termination') or (analysis.get('quality') or {}).get('status', 'UNVERIFIED') != 'PASS':
        final_state = 'COMPLETED_WITH_SCIENTIFIC_WARNINGS'
    else:
        final_state = 'SUCCESS'
    if transfer.get('failed'):
        final_state += '_PARTIAL_TRANSFER'
    _write_status('finished', final_state=final_state,
                  transfer=dict(transfer, state='partial' if transfer.get('failed') else 'complete'),
                  saved_output_bytes=_persisted_bytes(WORK_DIR))
    _deliver_final_metadata(results_dir, transfer)
    _JOB_FINALIZED = True
    send_msg('[Kaggle] Final status: ' + final_state +
             (' | Some artifacts need retry from the saved Kaggle output.' if transfer.get('failed') else ''))
    if calculation_cause != 'success':
        raise RuntimeError('Calculation ended with ' + calculation_cause + '; available files and status were preserved.')
    if report_error is not None:
        raise RuntimeError('Scientific report generation failed; raw files and diagnostics were preserved.') from report_error
except Exception as exc:
    if not _JOB_FINALIZED:
        _record_error('runner', exc)
        _write_status('failed', final_state='FAILED', runner_error_type=type(exc).__name__)
        results_dir = os.path.join(WORK_DIR, 'outputs')
        os.makedirs(results_dir, exist_ok=True)
        try:
            _collect_calculation_files(results_dir)
            _stage_scientific_report(globals().get('bundle'), results_dir)
            transfer = send_results_with_fallback('', results_dir)
            _write_status('failed', final_state='FAILED', transfer=dict(transfer,
                          state='partial' if transfer.get('failed') else 'complete'))
            _deliver_final_metadata(results_dir, transfer)
        except Exception as rescue_error:
            _record_error('failure_recovery', rescue_error)
    sanitized = _redact_message(exc)
    print('[Kaggle] Fatal job error: ' + sanitized, file=sys.stderr)
    send_msg('[Kaggle] Job failed: ' + sanitized[:1800] +
             '\nExact uploaded inputs, available raw results and job_status.json are preserved in notebook output.')
    # Never include a credential-bearing Requests exception as a chained cause.
    raise RuntimeError('ChemBot runner failed: ' + sanitized) from None
finally:
    if GAUSSIAN_SCRATCH_DIR:
        shutil.rmtree(GAUSSIAN_SCRATCH_DIR, ignore_errors=True)
'''

SELF_TEST_CODE = 'import base64, copy, hashlib, json, math, os, tempfile, unittest\nfrom pathlib import Path\nfrom unittest.mock import patch\nORCA = \'\'\'O   R   C   A\nProgram Version 6.1.0\n| 1> ! PBE0 def2-SVP Opt Freq\n| 2> * xyz 0 1\nYour calculation utilizes the basis: def2-SVP\nSCF CONVERGED AFTER 8 CYCLES\nFINAL SINGLE POINT ENERGY -75.000000000\nTHE OPTIMIZATION HAS CONVERGED\nCARTESIAN COORDINATES (ANGSTROEM)\nO 0.0 0.0 0.0\nH 0.8 0.0 0.6\nH -0.8 0.0 0.6\n\nVIBRATIONAL FREQUENCIES\n0: 0.0 cm**-1\n1: 0.0 cm**-1\n2: 0.0 cm**-1\n3: 0.0 cm**-1\n4: 0.0 cm**-1\n5: 0.0 cm**-1\n6: 1200.0 cm**-1\n7: 3200.0 cm**-1\n8: 3300.0 cm**-1\n\nIR SPECTRUM\nMode freq eps Int T**2 TX TY TZ\n6: 1200.0 0.001 10.0 0.002 (0.1 0.2 0.3)\n7: 3200.0 0.003 20.0 0.004 (0.2 0.3 0.4)\n8: 3300.0 0.005 30.0 0.006 (0.3 0.4 0.5)\n\nORBITAL ENERGIES\nNO OCC E(Eh) E(eV)\n0 2.0000 -1.000000 -27.211386\n1 2.0000 -0.250000 -6.802847\n2 0.0000 0.050000 1.360569\n\nABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS\nState Energy Wavelength fosc\n1 20000.0 500.0 0.2\n2 25000.0 400.0 0.1\n\nTHERMOCHEMISTRY AT 298.15K\nTemperature ... 298.15 K\nPressure ... 1.0 atm\nElectronic energy ... -75.000000000 Eh\nZero point energy ... 0.018 Eh\nTotal thermal energy -74.978 Eh\nThermal Enthalpy correction ... 0.00094421 Eh\nTotal Enthalpy ... -74.97705579 Eh\nFinal Gibbs free energy ... -75.0 Eh\nStandard-RRHO\nORCA TERMINATED NORMALLY\n\'\'\'\n\nPSI4 = \'\'\'Psi4: An Open-Source Ab Initio Electronic Structure Package\nPsi4 1.11\nset basis sto-3g\nReference: RHF\nGeometry (in Angstrom), charge = 0, multiplicity = 1:\nO 0.0 0.0 0.0\nH 0.8 0.0 0.6\nH -0.8 0.0 0.6\n\nEnergy and wave function converged.\n@RHF Final Energy: -75.000000\n==> Orbital Energies [Eh] <==\nDoubly Occupied:\n1A1 -1.0 2A1 -0.25\nVirtual:\n\n3A1 0.05 1B2 0.10\n\nProperty section\nOptimization is complete\nPsi4 exiting successfully\n\'\'\'\n\nGAUSSIAN = \'\'\'Gaussian 16: synthetic format regression\n #p b3lyp/6-31g(d) opt freq\n ------------------------------------\n Standard basis: 6-31G(d) (6D, 7F)\n Charge = 0 Multiplicity = 1\n SCF Done: E(RB3LYP) = -75.000000 A.U. after 10 cycles\n Optimization completed.\n Standard orientation:\n ---------------------------------------------------------------------\n Center Atomic Atomic Coordinates (Angstroms)\n Number Number Type X Y Z\n ---------------------------------------------------------------------\n 1 8 0 0.0 0.0 0.0\n 2 1 0 0.8 0.0 0.6\n 3 1 0 -0.8 0.0 0.6\n ---------------------------------------------------------------------\n Harmonic frequencies (cm**-1), IR intensities (KM/Mole)\n                      1             2              3\n Frequencies --   1200.0       3200.0         3300.0\n IR Inten    --     10.0         20.0           30.0\n - Thermochemistry -\n Temperature 298.15 Kelvin. Pressure 1.0 Atm.\n Zero-point correction= 0.018000 (Hartree/Particle)\n Thermal correction to Energy= 0.022000\n Thermal correction to Enthalpy= 0.02294421\n Thermal correction to Gibbs Free Energy= 0.000000\n Sum of electronic and zero-point Energies= -74.982000\n Sum of electronic and thermal Energies= -74.978000\n Sum of electronic and thermal Enthalpies= -74.97705579\n Sum of electronic and thermal Free Energies= -75.000000\n Normal termination of Gaussian 16\n\'\'\'\n\ndef parse(text=ORCA, name=\'water.out\'):\n    return A[\'parse_output_text\'](text, name)\n\n\nclass ScientificChecks(unittest.TestCase):\n    def test_orca_minimum_and_thermochemistry(self):\n        a = parse()\n        self.assertEqual(a[\'quality\'][\'status\'], \'PASS\')\n        self.assertEqual(a[\'expected_vibration_count\'], 3)\n        self.assertEqual(a[\'stationary_point_status\'], \'LIKELY_MINIMUM\')\n        self.assertTrue(a[\'quality\'][\'thermochemistry_reliable\'])\n        self.assertAlmostEqual(a[\'energies\'][\'final_energy_hartree\'], -75.0)\n\n    def test_source_hash(self):\n        self.assertEqual(parse()[\'source_sha256\'], hashlib.sha256(ORCA.encode()).hexdigest())\n\n    def test_finite_fortran_numbers(self):\n        self.assertEqual(A[\'_float\'](\'-1.5D+02\'), -150.)\n        self.assertIsNone(A[\'_float\'](\'NaN\'))\n        self.assertIsNone(A[\'_float\'](\'inf\'))\n\n    def test_termination_is_not_input_comment(self):\n        a = parse(ORCA.replace(\'ORCA TERMINATED NORMALLY\', \'| 9> # ORCA TERMINATED NORMALLY\'))\n        self.assertIsNone(a[\'normal_termination\'])\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_failed_calculation(self):\n        a = parse(ORCA.replace(\'ORCA TERMINATED NORMALLY\', \'ORCA finished by error termination\'))\n        self.assertEqual(a[\'quality\'][\'status\'], \'FAIL\')\n\n    def test_opt_failure_despite_normal_termination(self):\n        a = parse(ORCA.replace(\'THE OPTIMIZATION HAS CONVERGED\', \'The optimization did not converge\'))\n        self.assertEqual(a[\'quality\'][\'status\'], \'FAIL\')\n\n    def test_scf_failure_withholds_descriptors(self):\n        a = parse(ORCA.replace(\'SCF CONVERGED AFTER 8 CYCLES\', \'SCF NOT CONVERGED\'))\n        self.assertEqual(a[\'conceptual_dft\'], {})\n\n    def test_unknown_scf_withholds_minimum(self):\n        a = parse(ORCA.replace(\'SCF CONVERGED AFTER 8 CYCLES\', \'\'))\n        self.assertEqual(a[\'stationary_point_status\'], \'UNVERIFIED_ELECTRONIC_STATE\')\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_incomplete_frequencies(self):\n        a = parse(ORCA.replace(\'8: 3300.0 cm**-1\', \'\'))\n        self.assertFalse(a[\'quality\'][\'frequency_complete\'])\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_one_imaginary_is_candidate(self):\n        a = parse(ORCA.replace(\'6: 1200.0 cm**-1\', \'6: -120.0 cm**-1\'))\n        self.assertEqual(a[\'stationary_point_status\'], \'FIRST_ORDER_SADDLE_CANDIDATE\')\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_small_negative_is_not_ignored(self):\n        a = parse(ORCA.replace(\'6: 1200.0 cm**-1\', \'6: -3.0 cm**-1\'))\n        self.assertEqual(a[\'stationary_point_status\'], \'NEAR_ZERO_IMAGINARY_MODES\')\n\n    def test_linear_modes(self):\n        self.assertEqual(A[\'_molecule_mode_counts\']([dict(element=\'C\', x=0,y=0,z=0),dict(element=\'O\',x=0,y=0,z=1),dict(element=\'O\',x=0,y=0,z=-1)]), (3, True, 4))\n\n    def test_orca_bohr_conversion(self):\n        a = parse(ORCA.replace(\'(ANGSTROEM)\', \'(A.U.)\'))\n        self.assertAlmostEqual(a[\'final_geometry\'][1][\'x\'], .8*A[\'BOHR_TO_ANGSTROM\'])\n        self.assertEqual(a[\'source_geometry_units\'], \'bohr\')\n\n    def test_thermal_reference_mismatch(self):\n        a = parse(ORCA.replace(\'Electronic energy ... -75.000000000 Eh\', \'Electronic energy ... -76.000000000 Eh\'))\n        self.assertFalse(a[\'quality\'][\'thermochemical_energy_compatible\'])\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_missing_thermal_reference(self):\n        a = parse()\n        a[\'thermochemistry\'][\'electronic_energy_hartree\'] = None\n        a = A[\'validate_analysis\'](a)\n        self.assertIsNone(a[\'quality\'][\'thermochemical_energy_compatible\'])\n        self.assertFalse(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_orca_enthalpy_does_not_omit_zpe(self):\n        t = A[\'_orca_thermo_block\'](\'Electronic energy ... -75.0 Eh\\nThermal Enthalpy correction ... 0.001 Eh\')\n        self.assertIsNone(t[\'enthalpy_hartree\'])\n\n    def test_orca_enthalpy_from_total_u(self):\n        t = A[\'_orca_thermo_block\'](\'Electronic energy ... -75.0 Eh\\nTotal thermal energy -74.98 Eh\\nThermal Enthalpy correction ... 0.001 Eh\\nFinal entropy term ... 0.02 Eh\')\n        self.assertAlmostEqual(t[\'enthalpy_hartree\'], -74.979)\n        self.assertAlmostEqual(t[\'gibbs_hartree\'], -74.999)\n\n    def test_orca_enthalpy_from_total_correction(self):\n        t = A[\'_orca_thermo_block\'](\'Electronic energy ... -75.0 Eh\\nTotal correction 0.02 Eh\\nThermal Enthalpy correction ... 0.001 Eh\')\n        self.assertAlmostEqual(t[\'enthalpy_hartree\'], -74.979)\n\n    def test_orca_correction_fields_have_consistent_meanings(self):\n        t=parse()[\'thermochemistry\']\n        self.assertAlmostEqual(t[\'pv_correction_hartree\'],.00094421)\n        self.assertAlmostEqual(t[\'enthalpy_correction_hartree\'],.02294421)\n\n    def test_orca_solvent_protocol_blocks_gas_equilibrium(self):\n        a=parse(ORCA.replace(\'! PBE0 def2-SVP Opt Freq\',\'! PBE0 def2-SVP Opt Freq CPCM(Water)\'))\n        self.assertEqual(a[\'solvation_model\'],\'CPCM\')\n        self.assertEqual(a[\'solvent\'],\'Water\')\n        self.assertEqual(a[\'quality\'][\'standard_state\'],\'CONTINUUM_ELECTRONIC_PLUS_GAS_THERMAL_REFERENCE\')\n\n    def test_orca_ir_intensity_column(self):\n        self.assertEqual(tuple(parse()[\'ir_spectrum\'][0]), (1200.,10.))\n\n    def test_ir_missing_intensity_not_fabricated(self):\n        a = parse(ORCA.replace(\'Mode freq eps Int T**2 TX TY TZ\',\'unrecognized table header\'))\n        self.assertEqual(a[\'ir_spectrum\'], [])\n\n    def test_exact_correlated_method(self):\n        self.assertEqual(parse(ORCA.replace(\'! PBE0\', \'! DLPNO-CCSD(T1)\'))[\'method\'], \'DLPNO-CCSD(T1)\')\n\n    def test_orca_jobs_do_not_inherit(self):\n        a = parse(ORCA + \'\\nO   R   C   A\\n| 1> ! HF def2-SVP SP\\nSCF CONVERGED AFTER 4 CYCLES\\nFINAL SINGLE POINT ENERGY -74.0\\nORCA TERMINATED NORMALLY\\n\')\n        self.assertEqual(a[\'job_blocks_count\'], 2)\n        self.assertEqual(a[\'frequencies_cm1\'], [])\n        self.assertIsNone(a[\'thermochemistry\'][\'gibbs_hartree\'])\n\n    def test_psi4_energy_and_symmetry(self):\n        a = parse(PSI4,\'psi4.out\')\n        self.assertEqual(a[\'engine\'], \'Psi4\')\n        self.assertAlmostEqual(a[\'orbitals\'][\'homo_ev\'], -.25*A[\'HARTREE_TO_EV\'])\n        self.assertAlmostEqual(a[\'orbitals\'][\'lumo_ev\'], .05*A[\'HARTREE_TO_EV\'])\n\n    def test_psi4_correlated_energy_is_not_scf(self):\n        text = PSI4.replace(\'Psi4 exiting successfully\', \'MP2 total energy = -75.25\\nPsi4 exiting successfully\')\n        a = parse(text)\n        self.assertEqual(a[\'method\'], \'MP2\')\n        self.assertAlmostEqual(a[\'energies\'][\'final_energy_hartree\'], -75.25)\n\n    def test_psi4_bohr_units(self):\n        a = parse(PSI4.replace(\'(in Angstrom)\', \'(in Bohr)\'))\n        self.assertAlmostEqual(a[\'final_geometry\'][1][\'x\'], .8*A[\'BOHR_TO_ANGSTROM\'])\n\n    def test_psi4_explicit_ev_orbitals(self):\n        orbitals = A[\'_psi4_orbitals_v59\'](\'==> Orbital Energies [eV] <==\\nDoubly Occupied:\\n1A1 -5.0\\nVirtual:\\n2A1 1.0\\n\')\n        self.assertAlmostEqual(orbitals[\'homo_ev\'],-5.)\n        self.assertAlmostEqual(orbitals[\'lumo_ev\'],1.)\n\n    def test_psi4_spin_reset_for_final_table(self):\n        text=\'==> Orbital Energies [Eh] <==\\nAlpha Occupied: 1A1 -1.0\\nAlpha Virtual: 2A1 0.1\\nOther property\\n==> Orbital Energies [Eh] <==\\nDoubly Occupied: 1A1 -0.5\\nVirtual: 2A1 0.2\\n\'\n        orbitals=A[\'_psi4_orbitals_v59\'](text)\n        self.assertEqual(orbitals[\'orbitals\'][0][\'occ\'],2.)\n\n    def test_psi4_unknown_geometry_units(self):\n        a = parse(PSI4.replace(\'Geometry (in Angstrom)\', \'Final optimized geometry\'))\n        self.assertEqual(a[\'geometry_units\'], \'UNKNOWN\')\n        self.assertIsNone(a[\'expected_vibration_count\'])\n\n    def test_psi4_missing_cell_alignment(self):\n        f, ir = A[\'_parse_psi4_vibrations\'](\'Freq [cm^-1] 100.0 ---- 300.0\\nIR activ [km/mol] 10.0 20.0 30.0\\n\')\n        self.assertEqual(f,[100.,300.])\n        self.assertEqual(ir,[(100.,10.),(300.,30.)])\n\n    def test_psi4_multiple_jobs(self):\n        a = parse(PSI4 + \'\\nPsi4: An Open-Source Ab Initio Electronic Structure Package\\n@RHF Final Energy: -1.0\\nPsi4 exiting successfully\\n\')\n        self.assertEqual(a[\'job_blocks_count\'],2)\n        self.assertEqual(a[\'final_geometry\'],[])\n\n    def test_dipole_atomic_units(self):\n        d = A[\'_dipole_strict\'](\'Total Dipole Moment : -1.0 2.0 0.0\',\'ORCA\')\n        self.assertAlmostEqual(d[\'magnitude_debye\'],math.sqrt(5)*2.541746473)\n\n    def test_psi4_unknown_dipole_units(self):\n        self.assertEqual(A[\'_dipole_strict\'](\'Dipole Moment:\\nX: 1.0 Y: 2.0 Z: 0.0 Total: 2.2\',\'Psi4\'),{})\n\n    def test_gaussian_energy_and_thermal_sum(self):\n        a = parse(GAUSSIAN,\'water.log\')\n        self.assertEqual(a[\'engine\'],\'Gaussian\')\n        self.assertAlmostEqual(a[\'energies\'][\'final_energy_hartree\'],-75.)\n        self.assertTrue(a[\'quality\'][\'thermochemistry_reliable\'])\n\n    def test_gaussian_no_false_completion(self):\n        self.assertIsNone(parse(GAUSSIAN.replace(\'Normal termination of Gaussian 16\',\'\'))[\'normal_termination\'])\n\n    def test_gaussian_link1(self):\n        second = \'\\n--Link1--\\n #p hf/sto-3g sp\\n ----\\n SCF Done: E(RHF) = -74.0 A.U. after 3 cycles\\n Normal termination of Gaussian 16\\n\'\n        a = parse(GAUSSIAN + second)\n        self.assertEqual(a[\'job_blocks_count\'],2)\n        self.assertIsNone(a[\'thermochemistry\'][\'gibbs_hartree\'])\n\n    def test_gaussian_unsupported_composite(self):\n        a = parse(GAUSSIAN.replace(\'#p b3lyp/6-31g(d) opt freq\',\'#p ONIOM(b3lyp/6-31g(d):UFF) opt freq\'))\n        self.assertIsNone(a[\'energies\'][\'final_energy_hartree\'])\n\n    def test_reaction_balanced_and_comparable(self):\n        a = parse(); b = copy.deepcopy(a); b[\'filename\']=\'isomer.out\'\n        for k in (\'final_energy_hartree\',): b[\'energies\'][k]+=.01\n        for k in (\'electronic_energy_hartree\',\'electronic_reference_hartree\',\'enthalpy_hartree\',\'gibbs_hartree\'): b[\'thermochemistry\'][k]+=.01\n        b=A[\'validate_analysis\'](b)\n        r=A[\'evaluate_reaction_terms\']([(1,\'water\')],[(1,\'isomer\')],{\'water\':a,\'isomer\':b},\'water -> isomer\')\n        self.assertAlmostEqual(r[\'delta_e_kj_mol\'],.01*A[\'HARTREE_TO_KJMOL\'])\n        self.assertAlmostEqual(r[\'delta_g_kj_mol\'],.01*A[\'HARTREE_TO_KJMOL\'])\n        self.assertAlmostEqual(r[\'log10_keq\'],-.01*A[\'HARTREE_TO_KJMOL\']*1000/(A[\'GAS_CONSTANT_J_MOL_K\']*298.15*math.log(10)))\n\n    def test_reaction_mismatched_theory(self):\n        a=parse(); b=copy.deepcopy(a); b[\'basis\']=\'def2-TZVP\'\n        r=A[\'evaluate_reaction_terms\']([(1,\'a\')],[(1,\'b\')],{\'a\':a,\'b\':b},\'a -> b\')\n        self.assertIsNone(r[\'delta_e_kj_mol\']); self.assertIsNone(r[\'keq\'])\n\n    def test_reaction_unbalanced_atoms(self):\n        a=parse(); b=copy.deepcopy(a); b[\'final_geometry\'].pop()\n        with self.assertRaises(ValueError): A[\'evaluate_reaction_terms\']([(1,\'a\')],[(1,\'b\')],{\'a\':a,\'b\':b},\'a -> b\')\n\n    def test_reaction_unbalanced_charge(self):\n        a=parse(); b=copy.deepcopy(a); b[\'system\'][\'charge\']=1\n        with self.assertRaises(ValueError): A[\'evaluate_reaction_terms\']([(1,\'a\')],[(1,\'b\')],{\'a\':a,\'b\':b},\'a -> b\')\n\n    def test_nonfinite_is_json_null(self):\n        a=parse(); a[\'energies\'][\'extra\']=float(\'nan\')\n        result=A[\'validate_analysis\'](a)\n        self.assertIsNone(result[\'energies\'][\'extra\'])\n        json.dumps(result,allow_nan=False)\n\n    def test_spectrum_area_and_wavelength_jacobian(self):\n        import numpy as np\n        integrate = np.trapezoid if hasattr(np,\'trapezoid\') else np.trapz\n        grid=np.linspace(.001,5,30000)\n        density=A[\'_rf_gaussian_density\']([(2.,.2),(3.,.1)],grid,.15)\n        self.assertAlmostEqual(float(integrate(density,grid)),.3,places=6)\n        lam=(1239.841984/grid)[::-1]\n        transformed=(density*grid**2/1239.841984)[::-1]\n        self.assertAlmostEqual(float(integrate(transformed,lam)),.3,places=6)\n\n\nclass WorkerChecks(unittest.TestCase):\n    def test_gaussian_existing_installer_missing_interpreter_is_fixed(self):\n        import subprocess\n        with tempfile.TemporaryDirectory(prefix=\'gaussian test \') as d:\n            home=Path(d,\'g16\'); bsd=home/\'bsd\'; bsd.mkdir(parents=True)\n            installer=bsd/\'install\'\n            installer.write_bytes(b\'#!/missing/chembot/bin/sh\\r\\nprintf "installer-ready:%s\\\\n" "$g16root"\\r\\n\')\n            installer.chmod(0o750)\n            self.assertTrue(installer.is_file())\n            with self.assertRaises(FileNotFoundError):\n                subprocess.run([str(installer)],check=True)\n            self.assertEqual(R[\'_prepare_gaussian_runtime_files\'](str(home)),1)\n            with patch.dict(R,{\'WORK_DIR\':d}):\n                log=R[\'_run_gaussian_installer\'](str(installer),str(home),dict(os.environ,g16root=d))\n            self.assertIn(\'installer-ready:\'+d,Path(log).read_text())\n            self.assertNotIn(b\'\\r\\n\',installer.read_bytes())\n\n    def test_gaussian_runtime_binary_and_symlink_are_preserved(self):\n        import stat\n        with tempfile.TemporaryDirectory() as d:\n            home=Path(d,\'g16\'); bsd=home/\'bsd\'; bsd.mkdir(parents=True)\n            profile=bsd/\'g16.profile\'; profile.write_bytes(b\'\\xef\\xbb\\xbfexport g16root=/tmp\\r\\n\')\n            executable=home/\'l1.exe\'; raw=b\'\\x7fELF\\x00\\r\\n\\xff\'; executable.write_bytes(raw); executable.chmod(0o640)\n            link=home/\'linked.exe\'; link.symlink_to(\'l1.exe\')\n            self.assertEqual(R[\'_prepare_gaussian_runtime_files\'](str(home)),1)\n            self.assertEqual(executable.read_bytes(),raw)\n            self.assertTrue(link.is_symlink())\n            self.assertEqual(profile.read_bytes(),b\'export g16root=/tmp\\n\')\n            self.assertTrue(os.access(executable,os.X_OK))\n            for p in [home,bsd,profile,executable]:\n                self.assertEqual(stat.S_IMODE(p.stat().st_mode)&0o007,0)\n\n    def test_gaussian_csh_installer_honors_shell_and_flags(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_text(\'#!/missing/bin/csh -f\\nset hello = world\\n\')\n            with patch.object(R[\'shutil\'],\'which\',return_value=None), patch.dict(R,{\'_ensure_gaussian_csh\':lambda:\'/usr/bin/tcsh\'}):\n                self.assertEqual(R[\'_gaussian_install_command\'](str(p)),[\'/usr/bin/tcsh\',\'-f\',str(p)])\n\n    def test_gaussian_env_s_shebang_honors_shell(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_text(\'#!/usr/bin/env -S csh -f\\n\')\n            with patch.object(R[\'shutil\'],\'which\',return_value=None), patch.dict(R,{\'_ensure_gaussian_csh\':lambda:\'/usr/bin/tcsh\'}):\n                self.assertEqual(R[\'_gaussian_install_command\'](str(p)),[\'/usr/bin/tcsh\',\'-f\',str(p)])\n\n    def test_gaussian_csh_dependency_reused_without_install(self):\n        def forbidden(*args,**kwargs):\n            self.fail(\'An existing C shell must not trigger installation.\')\n        with patch.object(R[\'shutil\'],\'which\',return_value=\'/usr/bin/csh\'), patch.dict(R,{\'safe_install\':forbidden}):\n            self.assertEqual(R[\'_ensure_gaussian_csh\'](),\'/usr/bin/csh\')\n\n    def test_gaussian_missing_csh_is_installed_before_use(self):\n        calls=[]; installed=[False]\n        def which(name):\n            if name==\'apt-get\': return \'/usr/bin/apt-get\'\n            return \'/usr/bin/csh\' if installed[0] and name==\'csh\' else None\n        def install(command,**kwargs):\n            calls.append((command,kwargs))\n            if \'install\' in command: installed[0]=True\n        with patch.object(R[\'shutil\'],\'which\',side_effect=which), patch.object(R[\'os\'],\'geteuid\',return_value=0), patch.dict(R,{\'safe_install\':install}):\n            self.assertEqual(R[\'_ensure_gaussian_csh\'](),\'/usr/bin/csh\')\n        self.assertEqual(len(calls),2)\n        self.assertIn(\'update\',calls[0][0]); self.assertIn(\'tcsh\',calls[1][0])\n        self.assertTrue(all(call[1][\'max_seconds\']<=300 for call in calls))\n\n    def test_gaussian_missing_csh_without_privileges_has_clear_error(self):\n        def which(name): return \'/usr/bin/apt-get\' if name==\'apt-get\' else None\n        with patch.object(R[\'shutil\'],\'which\',side_effect=which), patch.object(R[\'os\'],\'geteuid\',return_value=1000):\n            with self.assertRaisesRegex(RuntimeError,\'csh.*missing\'):\n                R[\'_ensure_gaussian_csh\']()\n\n    def test_gaussian_csh_install_failure_is_not_hidden(self):\n        def which(name): return \'/usr/bin/apt-get\' if name==\'apt-get\' else None\n        def fail(*args,**kwargs): raise RuntimeError(\'test-only-secret: package unavailable\')\n        with patch.object(R[\'shutil\'],\'which\',side_effect=which), patch.object(R[\'os\'],\'geteuid\',return_value=0), patch.dict(R,{\'safe_install\':fail}):\n            with self.assertRaisesRegex(RuntimeError,\'C shell required\') as result:\n                R[\'_ensure_gaussian_csh\']()\n        self.assertNotIn(\'test-only-secret\',str(result.exception))\n        self.assertIn(\'package unavailable\',str(result.exception))\n\n    def test_gaussian_missing_installer_differs_from_broken_link(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\')\n            self.assertIsNone(R[\'_run_gaussian_installer\'](str(p),d,os.environ.copy()))\n            p.symlink_to(\'missing-target\')\n            with self.assertRaisesRegex(RuntimeError,\'broken link\'):\n                R[\'_run_gaussian_installer\'](str(p),d,os.environ.copy())\n\n    def test_gaussian_unknown_interpreter_has_clear_error(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_text(\'#!/missing/chembot_test_interpreter\\n\')\n            with self.assertRaisesRegex(RuntimeError,\'interpreter is missing\'):\n                R[\'_gaussian_install_command\'](str(p))\n\n    def test_gaussian_binary_installer_does_not_invoke_a_shell(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_bytes(b\'\\x7fELF\\x00binary\')\n            self.assertEqual(R[\'_gaussian_install_command\'](str(p)),[str(p)])\n\n    def test_gaussian_installer_nonzero_exit_preserves_stderr(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_text(\'#!/bin/sh\\necho installer-diagnostic >&2\\nexit 13\\n\')\n            with patch.dict(R,{\'WORK_DIR\':d}):\n                with self.assertRaisesRegex(RuntimeError,\'exit 13\') as result:\n                    R[\'_run_gaussian_installer\'](str(p),d,os.environ.copy())\n            self.assertIn(\'installer-diagnostic\',str(result.exception))\n            self.assertIn(\'installer-diagnostic\',Path(d,\'outputs\',\'gaussian_setup.log\').read_text())\n\n    def test_gaussian_installer_timeout_stops_the_process_group(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=Path(d,\'install\'); p.write_text(\'#!/bin/sh\\nexec sleep 60\\n\')\n            with patch.dict(R,{\'WORK_DIR\':d,\'_phase_timeout\':lambda *args,**kwargs:.1}):\n                with self.assertRaisesRegex(TimeoutError,\'bsd/install exceeded\'):\n                    R[\'_run_gaussian_installer\'](str(p),d,os.environ.copy())\n\n    def test_gaussian_maxdisk_bytes(self):\n        self.assertIn(\'MaxDisk=100000B\',R[\'_cap_gaussian_maxdisk\'](\'#p hf/sto-3g MaxDisk=100000B\\n\\nTitle\\n\'))\n\n    def test_gaussian_link1_disk_caps(self):\n        text=\'#p hf/sto-3g MaxDisk=1TB\\n\\nTitle\\n--Link1--\\n#p hf/sto-3g MaxDisk=100MB\\n\\nTitle\\n\'\n        capped=R[\'_cap_gaussian_maxdisk\'](text)\n        self.assertIn(\'MaxDisk=20000MB\',capped); self.assertIn(\'MaxDisk=100MB\',capped)\n\n    def test_orca_maxdisk_existing(self):\n        text=\'%scf\\n MaxDisk 1000\\n MaxIter 100\\nend\\n! HF\\n\'\n        self.assertEqual(R[\'_cap_orca_maxdisk\'](text),text)\n\n    def test_orca_inline_maxdisk_not_duplicated(self):\n        text=\'%scf MaxIter 100 MaxDisk 1000 end\\n! HF\\n\'\n        self.assertEqual(R[\'_cap_orca_maxdisk\'](text),text)\n\n    def test_exact_input_and_prepared_execution(self):\n        with tempfile.TemporaryDirectory() as d:\n            raw=\'! HF\\r\\n\'.encode(\'utf-16\')\n            R.update(WORK_DIR=d,INPUT_FILE=\'job.inp\',PREPARED_INPUT_TEXT=\'! HF\\n\',ENCODED_FILES_JSON=base64.b64encode(json.dumps({\'job.inp\':base64.b64encode(raw).decode()}).encode()).decode())\n            R[\'_stage_uploaded_inputs\']()\n            self.assertEqual(Path(d,\'outputs\',\'inputs\',\'job.inp\').read_bytes(),raw)\n            self.assertEqual(Path(d,\'job.inp\').read_bytes(),b\'! HF\\n\')\n\n    def test_lossless_file_parts(self):\n        with tempfile.TemporaryDirectory() as d:\n            R[\'WORK_DIR\']=d; p=Path(d,\'data.gbw\'); raw=bytes(range(256))*8; p.write_bytes(raw)\n            blocks=[]\n            for part,i,n in R[\'_iter_file_parts\'](str(p),chunk_bytes=300): blocks.append(Path(part).read_bytes())\n            self.assertEqual(b\'\'.join(blocks),raw)\n\n    def test_nested_artifacts_are_preserved(self):\n        with tempfile.TemporaryDirectory() as d:\n            R[\'WORK_DIR\']=d\n            source=Path(d,\'sub\',\'job.gbw\'); source.parent.mkdir(); source.write_bytes(b\'checkpoint\')\n            results=Path(d,\'outputs\'); results.mkdir()\n            R[\'_collect_calculation_files\'](str(results))\n            self.assertEqual((results/\'calculation/sub/job.gbw\').read_bytes(),b\'checkpoint\')\n\n    def test_report_paths_are_complete(self):\n        self.assertEqual(set(R[\'_bundle_file_paths\']({\'pdf\':\'p\',\'analysis_json\':\'j\',\'manifest\':\'m\',\'publication_files\':[\'a\'],\'csv_files\':{\'x\':\'c\'}})),{\'p\',\'j\',\'m\',\'a\',\'c\'})\n\n    def test_redaction(self):\n        self.assertNotIn(\'test-only-secret\',R[\'_redact_message\'](\'https://api.telegram.org/bottest-only-secret/sendDocument\'))\n\n\n'

exec(compile(ANALYZER_MODULE_CODE,'<scientific_analyzer>','exec'), globals())

def _run_self_tests():
    """Use isolated namespaces: no Telegram, Kaggle, auth or engine execution."""
    import ast
    import unittest
    namespace = {'__name__': 'chembot_self_test'}
    exec(compile(ANALYZER_MODULE_CODE, '<self_test_analyzer>', 'exec'), namespace)
    analyzer = dict(namespace)
    runner = {'__name__': 'chembot_self_test_worker', 'BOT_TOKEN': 'test-only-secret',
              'CHAT_ID': 1, 'JOB_ENGINE': 'orca', 'INPUT_FILE': 'job.inp',
              'KAGGLE_NOTEBOOK_URL': 'https://www.kaggle.com/code/test/job'}
    nodes = []
    for node in ast.parse(KAGGLE_RUNNER_CODE).body:
        if isinstance(node, ast.Try):
            break
        nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<self_test_worker>', 'exec'), runner)
    checks = {'__name__': 'chembot_self_test', 'A': analyzer, 'R': runner}
    exec(compile(SELF_TEST_CODE, '<self_test_suite>', 'exec'), checks)
    suite = unittest.TestSuite()
    for name in ('ScientificChecks', 'WorkerChecks'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(checks[name]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _offline_cli():
    import argparse
    parser = argparse.ArgumentParser(
        description='ChemBot 7.1.2: Telegram/Kaggle chemistry bot and conservative output analyzer.',
        epilog='Bot configuration: CHEMBOT_BOT_TOKEN, KAGGLE_USERNAME, KAGGLE_API_TOKEN (or KAGGLE_KEY), '
               'Optional owner overrides: CHEMBOT_ALLOWED_IDS=your Telegram user ID; CHEMBOT_ADMIN_ID=your ID. Original trusted IDs are used when omitted. '
               'Render Web Services automatically use HTTPS webhooks. Run without arguments to start the bot.')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--self-test', action='store_true', help='run synthetic scientific and worker regression checks without credentials')
    actions.add_argument('--analyze', metavar='OUTPUT', help='analyze one ORCA/Psi4/Gaussian text output locally, without bot credentials')
    actions.add_argument('--print-requirements', action='store_true', help='print Python package requirements suitable for a build step')
    parser.add_argument('--outdir', default='chembot_report', help='new/empty report destination for --analyze')
    args = parser.parse_args()
    if args.print_requirements:
        print('pyTelegramBotAPI>=4.16,<5\nkaggle>=2.2.4,<3\nmatplotlib>=3.7,<4\nreportlab>=4.0,<5\nnumpy>=1.24,<3\nrequests>=2.31,<3')
        return 0
    if args.self_test:
        ensure_dependencies(report_only=True)
        return _run_self_tests()
    if args.analyze:
        ensure_dependencies(report_only=True)
        path = Path(args.analyze).expanduser().resolve()
        if not path.is_file():
            parser.error('The analysis input is not a local file.')
        if path.stat().st_size > 128 * 1024 * 1024:
            parser.error('The analysis input exceeds 128 MiB; it will not be silently truncated.')
        outdir = Path(args.outdir).expanduser().resolve()
        if outdir.exists() and any(outdir.iterdir()):
            parser.error('--outdir must be new or empty to protect existing reports.')
        raw = path.read_bytes()
        text = decode_job_input(raw)
        analysis = parse_output_text(text, path.name)
        if analysis.get('engine') not in ('ORCA', 'Psi4', 'Gaussian'):
            parser.error('No supported chemistry engine was recognized in this output.')
        analysis['source_bytes_sha256'] = hashlib.sha256(raw).hexdigest()
        bundle = generate_report_bundle(analysis, outdir, path.stem)
        print(section_summary(analysis))
        print('Report:', bundle['pdf'])
        print('Analysis:', bundle['analysis_json'])
        print('Manifest:', bundle['manifest'])
        if bundle.get('figures_zip'):
            print('Publication exports:', bundle['figures_zip'])
        return 0
    parser.print_help()
    return 0


if __name__ == '__main__' and len(sys.argv) > 1:
    raise SystemExit(_offline_cli())

ensure_dependencies()

# ---------------------------------------------------------------------------
# Kaggle authentication: reproduce the working pattern of the original bot.
# Credentials/files are created BEFORE importing KaggleApi.
# ---------------------------------------------------------------------------
def _prepare_original_kaggle_auth():
    """Keep modern tokens and genuine legacy keys distinct; never derive either."""
    if not KAGGLE_USERNAME:
        return {"username":"", "modern_token":"", "legacy_key":"", "paths":[]}
    raw_token = (KAGGLE_API_TOKEN or "").strip()
    raw_key = (KAGGLE_KEY or "").strip()
    modern_token = ""
    legacy_key = ""
    for value in (raw_token, raw_key):
        if value.startswith("KGAT_"):
            modern_token = modern_token or value
        elif re.fullmatch(r"[0-9a-fA-F]{32}", value or ""):
            legacy_key = legacy_key or value
        elif value == raw_token and value:
            modern_token = value
    os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME
    if modern_token:
        os.environ["KAGGLE_API_TOKEN"] = modern_token
    else:
        os.environ.pop("KAGGLE_API_TOKEN", None)
    if legacy_key:
        os.environ["KAGGLE_KEY"] = legacy_key
    else:
        os.environ.pop("KAGGLE_KEY", None)
    written = []
    kaggle_home = Path.home()
    for directory in (kaggle_home / ".config" / "kaggle", kaggle_home / ".kaggle"):
        directory.mkdir(parents=True, exist_ok=True)
        if legacy_key:
            path = directory / "kaggle.json"
            path.write_text(json.dumps({"username":KAGGLE_USERNAME,"key":legacy_key}), encoding="utf-8")
            path.chmod(0o600)
            written.append(str(path))
    if modern_token:
        path = kaggle_home / ".kaggle" / "access_token"
        path.write_text(modern_token, encoding="utf-8")
        path.chmod(0o600)
        written.append(str(path))
    return {"username":KAGGLE_USERNAME,"modern_token":modern_token,"legacy_key":legacy_key,"paths":written}


KAGGLE_AUTH_INFO = _prepare_original_kaggle_auth()

# IMPORTANT: KaggleApi is imported only after the username/token/key and
# credential files above already exist, exactly as requested.
import telebot
from telebot import types

if not BOT_TOKEN:
    raise RuntimeError("Set CHEMBOT_BOT_TOKEN before running the bot.")

# Preserve the explicitly trusted users/chats from the supplied original bot.
# Environment settings can replace the owner list; permitted groups still do
# not authorize their other members to run jobs on the personal Kaggle account.
_DEFAULT_ALLOWED_IDS = {7495822836, -1003907097817, 839801823, -1003925918657, -1003875125323}
_DEFAULT_ADMIN_ID = 839801823

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


def _resolve_authorization(environ=None):
    """Retain original defaults while respecting explicit owner overrides."""
    environ = os.environ if environ is None else environ
    def value(name):
        return str(environ.get(name, '') or '').strip().strip('"').strip("'")
    raw_ids = value('CHEMBOT_ALLOWED_IDS')
    raw_admin = value('CHEMBOT_ADMIN_ID')
    if raw_ids:
        ids = _parse_id_set(raw_ids, set(), 'CHEMBOT_ALLOWED_IDS')
    elif raw_admin and raw_admin != '0':
        # An explicit administrator replaces the original trusted users.
        ids = {identifier for identifier in _DEFAULT_ALLOWED_IDS if identifier < 0}
    else:
        ids = set(_DEFAULT_ALLOWED_IDS)
    if 0 in ids:
        raise RuntimeError('CHEMBOT_ALLOWED_IDS must contain non-zero numeric user/chat IDs.')
    if raw_admin:
        try:
            admin = int(raw_admin)
        except ValueError as exc:
            raise RuntimeError('CHEMBOT_ADMIN_ID must be a Telegram numeric ID.') from exc
        if admin < 0:
            raise RuntimeError('CHEMBOT_ADMIN_ID must be a positive user ID, not a group ID.')
    elif raw_ids and any(identifier > 0 for identifier in ids):
        # A custom user list must not silently retain an unrelated default admin.
        admin = 0
    else:
        admin = _DEFAULT_ADMIN_ID
        # Existing Render installations sometimes configured only chat IDs.
        # Reuse the original trusted users, rather than granting access to every
        # member of those groups or making new variables mandatory at startup.
        ids.update(identifier for identifier in _DEFAULT_ALLOWED_IDS if identifier > 0)
    if admin > 0:
        ids.add(admin)
    if not any(identifier > 0 for identifier in ids):
        raise RuntimeError('Authorization has no permitted user. Add a positive user ID to CHEMBOT_ALLOWED_IDS or CHEMBOT_ADMIN_ID.')
    return ids, admin


ALLOWED_IDS, ADMIN_ID = _resolve_authorization()
ORCA_DATASET_SLUG = os.environ.get(
    "ORCA_DATASET_SLUG", "abdulsalsmsalih/orca-6-1-0"
)
GAUSSIAN_DATASET_SLUG = os.environ.get(
    "GAUSSIAN_DATASET_SLUG",
    f"{KAGGLE_USERNAME or 'abdulsalsmsalih'}/gauusian16",
)
MAX_TELEGRAM_DOWNLOAD = 20_000_000
MAX_AUX_STORAGE = 20 * 1024 * 1024
MAX_TEXT_OUT = 20_000_000

user_aux_storage = {}
user_drive_links = {}
recent_kaggle_jobs = {}
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
    roots = [(Path(tempfile.gettempdir()), 21600.0)]
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
                if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith(LEGACY_JOB_PREFIXES):
                    continue
                marker = entry / '.chembot-staging'
                if not marker.is_file() or marker.read_text(encoding='utf-8') != 'ChemBot staging directory':
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


def snapshot_user_job_context(chat_id, user_id):
    """Snapshot the preparation context belonging to this user and chat."""
    key = (chat_id, user_id)
    with state_lock:
        aux = dict(user_aux_storage.get(key, {}))
        drive = str(user_drive_links.get(key, '') or '')
    return aux, drive

# ============================================================
# Shared analyzer source. It is executed locally and injected into Kaggle jobs.
# ============================================================


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

def _redact_kaggle_error(text):
    rendered = str(text or "")
    secrets = [BOT_TOKEN, KAGGLE_API_TOKEN, KAGGLE_KEY,
               KAGGLE_AUTH_INFO.get("modern_token"), KAGGLE_AUTH_INFO.get("legacy_key")]
    for secret in secrets:
        if secret and len(secret) >= 8:
            rendered = rendered.replace(secret, "[REDACTED]")
    return re.sub(r"https://api[.]telegram[.]org/bot[^/\s]+", "https://api.telegram.org/bot[REDACTED]", rendered)


def _auth_mode_summary():
    if KAGGLE_AUTH_INFO.get('modern_token') and KAGGLE_AUTH_INFO.get('legacy_key'):
        return 'modern token + genuine legacy credentials'
    if KAGGLE_AUTH_INFO.get('modern_token'):
        return 'modern API token'
    if KAGGLE_AUTH_INFO.get('legacy_key'):
        return 'legacy username/key'
    return 'not configured'


def _new_authenticated_kaggle_api(api_factory=None):
    if not KAGGLE_USERNAME:
        raise RuntimeError("KAGGLE_USERNAME is empty.")
    if not (KAGGLE_AUTH_INFO.get("modern_token") or KAGGLE_AUTH_INFO.get("legacy_key")):
        raise RuntimeError("Set KAGGLE_API_TOKEN or a genuine legacy KAGGLE_KEY.")
    if api_factory is None:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api_factory = KaggleApi
    client = api_factory()
    try:
        client.authenticate()
    except SystemExit as exc:
        raise RuntimeError("Kaggle credential loading failed; check the token/key and username.") from exc
    actual_user = (getattr(client, 'config_values', None) or {}).get('username')
    if actual_user and str(actual_user).lower() != KAGGLE_USERNAME:
        raise RuntimeError('Kaggle credentials belong to a different username than KAGGLE_USERNAME.')
    return client


def submit_kaggle_job(input_name, encoded_files_json, chat_id, is_psi4, extras, drive_link, api_factory=None, is_gaussian=False, prepared_input_text=None):
    """Package and push a Kaggle kernel using the original bot's KaggleApi flow."""
    job_id = 'chem-job-' + uuid.uuid4().hex[:16]
    job_dir = None
    try:
        job_dir = tempfile.mkdtemp(prefix=job_id + '_')
        Path(job_dir, '.chembot-staging').write_text('ChemBot staging directory', encoding='utf-8')
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
            'INPUT_FILE=' + repr(os.path.basename(str(input_name).replace('\\', '/'))) + '\n'
            'PREPARED_INPUT_TEXT=' + repr(prepared_input_text) + '\n'
            'ENCODED_FILES_JSON=' + repr(encoded_files_json) + '\n'
            'DRIVE_LINK=' + repr(drive_link) + '\n'
            'PSI4_EXTRAS=' + repr(extras) + '\n'
            'JOB_ENGINE=' + repr(engine) + '\n'
            'KAGGLE_NOTEBOOK_URL=' + repr(f'https://www.kaggle.com/code/{KAGGLE_USERNAME}/{job_id}') + '\n'
        )
        analyzer_code = ANALYZER_MODULE_CODE
        script = 'from __future__ import annotations\n' + header + '\n' + analyzer_code.replace('from __future__ import annotations\n', '') + '\n' + KAGGLE_RUNNER_CODE
        if is_gaussian:
            notebook = {
                'cells': [{
                    'cell_type': 'code',
                    'id': 'chemrun-' + uuid.uuid4().hex[:16],
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
            value = response.get(attr) if isinstance(response, dict) else getattr(response, attr, None)
            if value:
                if isinstance(value, (list, tuple)):
                    value = ', '.join(map(str, value))
                problems.append(f'{label}: {value}')
        if problems:
            raise RuntimeError(' ; '.join(problems))

        # Prefer the actual response URL/ref when the API supplies it.
        url = response.get('url') if isinstance(response, dict) else getattr(response, 'url', None)
        ref = response.get('ref') if isinstance(response, dict) else getattr(response, 'ref', None)
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

# Per-conversation preparation ordering and publication downloads.
# Tickets are reserved at update ingress, before threaded handlers download.
# The Condition is released throughout downloads, rendering and Kaggle push.
_context_condition = threading.Condition(threading.RLock())
_context_work_queues = {}
_update_dispatch_lock = threading.RLock()
_seen_update_ids = OrderedDict()


def _context_key(message):
    return (message.chat.id, message.from_user.id)


def _reserve_context_work(message):
    key = _context_key(message)
    ticket = object()
    with _context_condition:
        _context_work_queues.setdefault(key, []).append(ticket)
    message._chembot_context_ticket = (key, ticket)
    return key, ticket


def _begin_context_work(key, ticket):
    with _context_condition:
        _context_condition.wait_for(
            lambda: bool(_context_work_queues.get(key))
            and _context_work_queues[key][0] is ticket
        )


def _finish_context_work(key, ticket):
    with _context_condition:
        queue = _context_work_queues.get(key, [])
        if ticket in queue:
            queue.remove(ticket)
        if not queue:
            _context_work_queues.pop(key, None)
        _context_condition.notify_all()


def _has_context_handler(message):
    if not getattr(message, 'from_user', None) or not getattr(message, 'chat', None):
        return False
    if getattr(message, 'document', None) is not None:
        return True
    text = getattr(message, 'text', None) or ''
    command = text.split()[0].split('@')[0].lower() if text.split() else ''
    return command in {'/start', '/clearaux', '/jobs', '/reaction', '/export', '/publication'} or text.startswith(('http://', 'https://'))


def _install_context_ordering(bot_instance):
    original = bot_instance.process_new_updates

    def process_ordered(updates):
        # Telegram can retry a webhook after a slow/lost HTTP acknowledgment.
        # Deduplicate ingress before reserving tickets or starting Kaggle jobs.
        with _update_dispatch_lock:
            fresh, new_ids, reservations = [], [], []
            for update in updates:
                update_id = getattr(update, 'update_id', None)
                if update_id is not None and (update_id in _seen_update_ids or update_id in new_ids):
                    continue
                fresh.append(update)
                if update_id is not None:
                    new_ids.append(update_id)
                message = getattr(update, 'message', None)
                if message is not None and _has_context_handler(message):
                    if getattr(message, '_chembot_context_ticket', None) is None:
                        reservations.append(_reserve_context_work(message))
                    _register_analysis_group(message)
            if not fresh:
                return None
            try:
                result = original(fresh)
            except Exception:
                for key, ticket in reservations:
                    _finish_context_work(key, ticket)
                raise
            for update_id in new_ids:
                _seen_update_ids[update_id] = time.time()
            while len(_seen_update_ids) > 10000:
                _seen_update_ids.popitem(last=False)
            return result

    bot_instance.process_new_updates = process_ordered


def _ordered_context_handler(function):
    def ordered(message):
        if not getattr(message, 'from_user', None) or not getattr(message, 'chat', None):
            return function(message)
        reservation = getattr(message, '_chembot_context_ticket', None)
        if reservation is None:
            reservation = _reserve_context_work(message)
        _register_analysis_group(message)
        key, ticket = reservation
        _begin_context_work(key, ticket)
        try:
            return function(message)
        finally:
            _finish_analysis_group_error(message)
            _finish_context_work(key, ticket)
            message._chembot_context_ticket = None
    ordered.__name__ = function.__name__
    ordered.__doc__ = function.__doc__
    return ordered


def _start_analysis_group_timer(key, batch):
    timer = batch.get('timer')
    if timer is not None:
        timer.cancel()
    timer = threading.Timer(ANALYSIS_GROUP_DEBOUNCE_SECONDS, _finalize_analysis_group, args=(key,))
    timer.daemon = True
    batch['timer'] = timer
    timer.start()


def _register_analysis_group(message):
    gid = getattr(message, 'media_group_id', None)
    document = getattr(message, 'document', None)
    filename = getattr(document, 'file_name', '') or ''
    if not gid or not filename.lower().endswith(('.out','.log')):
        return None
    if getattr(message, '_chembot_group_registration', None) is not None:
        return message._chembot_group_registration[0]
    key = (message.chat.id, message.from_user.id, str(gid))
    registration = uuid.uuid4().hex
    with analysis_group_lock:
        batch = analysis_group_batches.setdefault(key, {
            'chat_id': message.chat.id, 'user_id': message.from_user.id,
            'group_id': str(gid), 'sids': [], 'pending': set(), 'timer': None,
        })
        batch.setdefault('pending', set()).add(registration)
        if batch.get('timer') is not None:
            batch['timer'].cancel()
            batch['timer'] = None
    message._chembot_group_registration = (key, registration)
    message._chembot_group_completed = False
    return key


def _finish_analysis_group_error(message):
    registration = getattr(message, '_chembot_group_registration', None)
    if not registration or getattr(message, '_chembot_group_completed', False):
        return
    key, ticket = registration
    with analysis_group_lock:
        batch = analysis_group_batches.get(key)
        if batch is not None:
            batch.setdefault('pending', set()).discard(ticket)
            if not batch['pending']:
                if batch.get('sids'):
                    _start_analysis_group_timer(key, batch)
                else:
                    analysis_group_batches.pop(key, None)
    message._chembot_group_completed = True


def _callback_owner_allowed(call, sess):
    sender = getattr(call, 'from_user', None)
    message = getattr(call, 'message', None)
    chat = getattr(message, 'chat', None)
    uid = getattr(sender, 'id', None)
    chat_id = getattr(chat, 'id', None)
    if uid is None or chat_id != sess.get('chat_id'):
        return False
    permitted = uid in ALLOWED_IDS or uid == ADMIN_ID
    return permitted and (uid == sess.get('user_id') or uid == ADMIN_ID)


def _publication_export_paths(sess):
    bundle = sess.get('bundle') or {}
    candidates = []
    for key in ('publication_files', 'csv_files'):
        values = bundle.get(key) or []
        if isinstance(values, dict):
            values = list(values.values())
        if isinstance(values, (str, os.PathLike)):
            values = [values]
        candidates.extend(values)
    for key, value in bundle.items():
        if ('csv' in str(key).lower() or key == 'figures_zip') and isinstance(value, (str, os.PathLike)):
            candidates.append(value)
    # Fallback for scientific reporters that return plots plus companion files.
    for value in (sess.get('plots') or {}).values():
        if isinstance(value, (str, os.PathLike)):
            stem = os.path.splitext(str(value))[0]
            candidates.extend(stem + suffix for suffix in ('.svg', '.pdf', '.csv', '.png', '_curve.csv', '_sticks.csv', '_plot_settings.json'))
    paths = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, (str, os.PathLike)):
            continue
        path = os.path.abspath(os.fspath(candidate))
        if path not in seen and os.path.isfile(path):
            seen.add(path)
            paths.append(path)
    return paths


def _send_publication_exports(chat_id, sess, individual=False):
    paths = _publication_export_paths(sess)
    if not paths:
        bot.send_message(chat_id, 'No publication files are available for this analysis.')
        return False
    if individual:
        files = [path for path in paths if not path.lower().endswith('.zip')]
        for path in files:
            with open(path, 'rb') as handle:
                bot.send_document(chat_id, handle, caption=os.path.basename(path))
        return bool(files)
    supplied_archive = (sess.get('bundle') or {}).get('figures_zip')
    if (isinstance(supplied_archive, (str, os.PathLike)) and os.path.isfile(supplied_archive)
            and not (sess.get('bundle') or {}).get('publication_exports_dirty')):
        with open(supplied_archive, 'rb') as handle:
            bot.send_document(chat_id, handle, caption='Publication exports: original figures and numerical source data.')
        return True
    import zipfile
    archive = os.path.join(sess['dir'], 'publication_exports.zip')
    # Build the complete archive from this bundle; supplied figures ZIPs can
    # themselves be included without dropping CSVs and later overlay exports.
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        names = set()
        for index, path in enumerate(paths, 1):
            if path.lower().endswith('.zip') or os.path.realpath(path) == os.path.realpath(archive):
                continue
            name = os.path.basename(path)
            if name in names:
                name = str(index) + '_' + name
            names.add(name)
            output.write(path, arcname=name)
    with open(archive, 'rb') as handle:
        bot.send_document(chat_id, handle, caption='Publication exports: original figures and numerical source data.')
    return True


def _register_overlay_exports(sess, image_path):
    if not image_path:
        return
    stem = os.path.splitext(str(image_path))[0]
    with session_lock:
        bundle = sess.setdefault('bundle', {})
        files = bundle.setdefault('publication_files', [])
        if not isinstance(files, list):
            files = list(files) if isinstance(files, (tuple, set)) else []
            bundle['publication_files'] = files
        for suffix in ('.svg', '.pdf', '.csv', '.png', '_curve.csv', '_sticks.csv', '_plot_settings.json'):
            path = stem + suffix
            if os.path.isfile(path) and path not in files:
                files.append(path)
                bundle['publication_exports_dirty'] = True

class _RedactedTelegramErrors:
    def handle(self, exception):
        print('Telegram handler error:', _redact_kaggle_error(exception))
        return True


bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=BOT_WORKER_THREADS,
                      exception_handler=_RedactedTelegramErrors())

_install_context_ordering(bot)
try:
    if not KAGGLE_USERNAME or not (KAGGLE_AUTH_INFO.get('modern_token') or KAGGLE_AUTH_INFO.get('legacy_key')):
        raise RuntimeError('Kaggle username and token/key are not configured.')
    KAGGLE_STARTUP_AUTH_OK = True
    KAGGLE_STARTUP_AUTH_ERROR = ''
except Exception as _auth_exc:
    KAGGLE_STARTUP_AUTH_OK = False
    KAGGLE_STARTUP_AUTH_ERROR = _redact_kaggle_error(_auth_exc)
print(f"CHEMBOT v7.1.2 is initialized with {BOT_WORKER_THREADS} Telegram workers; Kaggle credentials={'configured' if KAGGLE_STARTUP_AUTH_OK else 'FAILED'}")


def authorized(message):
    chat = getattr(message, 'chat', None)
    sender = getattr(message, 'from_user', None)
    chat_id = getattr(chat, 'id', None)
    user_id = getattr(sender, 'id', None)
    # A permitted group is a location, not permission for all of its members.
    return bool(user_id is not None and (user_id in ALLOWED_IDS or user_id == ADMIN_ID)
                and (chat_id is not None and (chat_id > 0 or chat_id in ALLOWED_IDS)))


def _send_submission_link(chat_id, message_text):
    """Send the accepted Kaggle URL from Render, before notebook setup runs."""
    for attempt in range(3):
        try:
            bot.send_message(chat_id,message_text,disable_web_page_preview=True)
            return True
        except Exception:
            if attempt<2: time.sleep(2**attempt)
    print('Telegram delivery failed for a Kaggle submission link; /jobs can retrieve it.')
    return False


def _own_kaggle_job_links():
    """Recover recent private ChemBot jobs from the authenticated Kaggle account."""
    api=_new_authenticated_kaggle_api()
    try:
        listed=api.kernels_list(mine=True,search='chem-job-',page=1,sort_by='dateRun')
    except TypeError:
        try: listed=api.kernels_list(mine=True,search='chem-job-',page=1)
        except TypeError: listed=api.kernels_list(mine=True,page=1)
    items=(listed.get('kernels',[]) if isinstance(listed,dict)
           else getattr(listed,'kernels',listed)) or []
    links=[]
    for item in items:
        ref=item.get('ref') if isinstance(item,dict) else getattr(item,'ref',None)
        if not isinstance(ref,str): continue
        owner,sep,slug=ref.partition('/')
        if sep and owner.lower()==KAGGLE_USERNAME.lower() and slug.lower().startswith('chem-job-'):
            links.append('https://www.kaggle.com/code/'+owner+'/'+slug)
    return links[:10]


@bot.message_handler(commands=['jobs'])
@_ordered_context_handler
def jobs_command(message):
    if not authorized(message): return
    uid=message.from_user.id; chat_id=message.chat.id
    with state_lock:
        links=list(recent_kaggle_jobs.get((chat_id,uid),[]))
    if uid==ADMIN_ID:
        try:
            for url in _own_kaggle_job_links():
                if url not in links: links.append(url)
        except Exception as exc:
            print('Could not list Kaggle jobs:',type(exc).__name__)
    bot.reply_to(message,'Recent private Kaggle jobs:\n'+'\n'.join(links[:10]) if links else 'No recent ChemBot job links were found.')


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
        ("📄 Full PDF", "pdf"), ("🖼 Original plots", "plots"),
        ("📦 Publication ZIP", "pub"), ("📄 Figure/data files", "pubfiles"),
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
    kb = types.InlineKeyboardMarkup(row_width=2)
    choices = [
        ('TD-DFT normalized σ0.15 eV', 'uv15n'),
        ('TD-DFT raw σ0.15 eV', 'uv15r'),
        ('TD-DFT normalized σ0.30 eV', 'uv30n'),
        ('FT-IR normalized σ12 cm⁻¹', 'ir12n'),
        ('FT-IR raw σ12 cm⁻¹', 'ir12r'),
        ('FT-IR normalized σ20 cm⁻¹', 'ir20n'),
        ('Clear older analyses', 'clear'),
    ]
    for label, action in choices:
        kb.add(types.InlineKeyboardButton(label, callback_data=f'c:{session_id}:{action}'))
    return kb

def _comparison_analyses(sess, kind):
    with session_lock:
        rows=[x for x in analysis_sessions.values() if x.get('chat_id')==sess.get('chat_id') and x.get('user_id')==sess.get('user_id')]
    rows=sorted(rows,key=lambda x:x.get('created',0))[-10:]
    rows=[x for x in rows if x['analysis'].get('tddft_states' if kind=='uv' else 'ir_spectrum')]
    return [x['analysis'] for x in rows]

def _send_comparison(chat_id, sess, action):
    if action == 'clear':
        with render_lock:
            with session_lock:
                stale = [(sid, row) for sid, row in analysis_sessions.items()
                         if row is not sess and row.get('chat_id') == sess.get('chat_id')
                         and row.get('user_id') == sess.get('user_id')]
                for sid, row in stale:
                    analysis_sessions.pop(sid, None)
            for _, row in stale:
                shutil.rmtree(row.get('dir', ''), ignore_errors=True)
        bot.send_message(chat_id, 'Older comparison analyses cleared.')
        return
    choices = {
        'uv15n': ('uv', 0.15, True), 'uv15r': ('uv', 0.15, False),
        'uv30n': ('uv', 0.30, True), 'uv30r': ('uv', 0.30, False),
        'ir12n': ('ir', 12.0, True), 'ir12r': ('ir', 12.0, False),
        'ir20n': ('ir', 20.0, True), 'ir20r': ('ir', 20.0, False),
        # Old message buttons retain usable actions with the corrected units.
        'uv10n': ('uv', 0.15, True), 'uv10r': ('uv', 0.15, False),
        'uv20n': ('uv', 0.30, True), 'uv20r': ('uv', 0.30, False),
    }
    if action not in choices:
        bot.send_message(chat_id, 'Unknown comparison action. Open the comparison menu again.')
        return
    kind, sigma, normalize = choices[action]
    analyses = _comparison_analyses(sess, kind)
    if len(analyses) < 2:
        bot.send_message(chat_id, 'Upload at least two compatible .out files for this spectrum type.')
        return
    sigma_slug = f'{sigma:g}'.replace('.', 'p')
    out = os.path.join(sess['dir'], f'comparison_{kind}_{sigma_slug}_{"norm" if normalize else "raw"}.png')
    with render_lock:
        path = make_overlay_plot(analyses, kind, out, normalize=normalize, sigma=sigma)
    if not path:
        bot.send_message(chat_id, 'Could not create an overlay from the parsed spectra.')
        return
    _register_overlay_exports(sess, path)
    label = 'TD-DFT / UV-Vis' if kind == 'uv' else 'FT-IR'
    unit = 'eV' if kind == 'uv' else 'cm⁻¹'
    with open(path, 'rb') as handle:
        bot.send_document(chat_id, handle, caption=f'{label} overlay · {len(analyses)} files · σ={sigma:g} {unit} · {"normalized" if normalize else "raw"}')

def create_analysis_session(chat_id, user_id, filename, text, source_bytes=None):
    a=parse_output_text(text,filename)
    if a.get('engine') not in ('ORCA','Psi4','Gaussian'):
        raise ValueError('Could not identify this .out file as ORCA, Psi4 or Gaussian output. Send the original text output, not a converted/trimmed report.')
    if source_bytes is not None:
        a['source_bytes_sha256'] = hashlib.sha256(source_bytes).hexdigest()
    sid=uuid.uuid4().hex[:10]
    d=tempfile.mkdtemp(prefix='chembot_analysis_')
    try:
        with render_lock:
            bundle=generate_report_bundle(a,d,Path(filename).stem)
            plots=bundle['plots']; pdf=bundle['pdf']
    except Exception:
        _safe_rmtree(d)
        raise
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
        own = sorted((key for key, row in analysis_sessions.items()
                      if row.get('chat_id') == chat_id and row.get('user_id') == user_id),
                     key=lambda key: analysis_sessions[key]['created'])
        for old in own[:-50]:
            _safe_rmtree(analysis_sessions.pop(old)['dir'])
    return sid,a


def _recent_analyses(chat_id, user_id, max_age_seconds=600, max_items=10):
    now=time.time()
    with session_lock:
        rows=[x for x in analysis_sessions.values() if x.get('chat_id')==chat_id and x.get('user_id')==user_id and now-x.get('created',0)<=max_age_seconds]
    return sorted(rows,key=lambda x:x.get('created',0))[-max_items:]


def _send_group_overlays(chat_id, sessions, group_id):
    analyses = [row['analysis'] for row in sessions]
    sess = sessions[-1]
    produced = []
    for kind, sigma, unit, label in [('uv', 0.15, 'eV', 'TD-DFT / UV-Vis'), ('ir', 12.0, 'cm⁻¹', 'FT-IR')]:
        compatible = [a for a in analyses if a.get('tddft_states' if kind == 'uv' else 'ir_spectrum')]
        if len(compatible) < 2:
            continue
        out = os.path.join(sess['dir'], f'group_{group_id}_{kind}_overlay.png')
        with render_lock:
            path = make_overlay_plot(compatible, kind, out, normalize=True, sigma=sigma)
        if path:
            _register_overlay_exports(sess, path)
            names = ', '.join(Path(a.get('filename', 'spectrum')).stem for a in compatible)
            caption = f'{label} comparison · {len(compatible)} files · σ={sigma:g} {unit}\n{names}'
            with open(path, 'rb') as handle:
                bot.send_document(chat_id, handle, caption=caption[:1024])
            produced.append(label)
    return produced


def _finalize_analysis_group(key):
    with analysis_group_lock:
        batch=analysis_group_batches.get(key)
        if batch and batch.get('pending'): return
        batch=analysis_group_batches.pop(key,None)
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


def _queue_analysis_group(message, sid):
    key = _register_analysis_group(message)
    if key is None:
        return False
    _, ticket = message._chembot_group_registration
    with analysis_group_lock:
        batch = analysis_group_batches.get(key)
        if batch is None:
            return False
        if sid not in batch['sids']:
            batch['sids'].append(sid)
        batch.setdefault('pending', set()).discard(ticket)
        if not batch['pending']:
            _start_analysis_group_timer(key, batch)
    message._chembot_group_completed = True
    return True


def _parse_reaction_equation(equation):
    parts=re.split(r"\s*(?:<=>|<->|-->|->|=>|=)\s*",equation.strip())
    if len(parts)!=2 or not all(parts):
        raise ValueError("Use reaction syntax: A + 2 B -> C + D")
    def side(value):
        terms=re.split(r"\s+\+\s+",value.strip())
        if len(terms)==1 and "+" in value and not value.rstrip().endswith("+"):
            terms=re.split(r"\s*\+\s*",value)
        result=[]
        for term in terms:
            match=re.fullmatch(r"(?:(\d+(?:[.]\d+)?)\s*(?:\*\s*|\s+))?(.+)",term.strip())
            if not match:
                raise ValueError("Could not parse reaction term: "+term)
            coefficient=float(match.group(1) or 1.0)
            if not math.isfinite(coefficient) or coefficient<=0:
                raise ValueError("Stoichiometric coefficients must be positive and finite.")
            result.append((coefficient,match.group(2).strip()))
        return result
    return side(parts[0]),side(parts[1])


def _reaction_thermo_from_sessions(chat_id,user_id,equation):
    reactants,products=_parse_reaction_equation(equation)
    rows=_recent_analyses(chat_id,user_id,max_age_seconds=21600,max_items=50)
    byname={Path(row["analysis"].get("filename","")).stem.lower():row["analysis"] for row in rows}
    return evaluate_reaction_terms(reactants,products,byname,equation)


@bot.message_handler(commands=['reaction'])
@_ordered_context_handler
def reaction_command(message):
    if not authorized(message): return
    equation=(message.text or '').partition(' ')[2].strip()
    if not equation:
        bot.reply_to(message,'Usage: /reaction A + B -> C + D\nNames must match uploaded output filename stems; separate terms with spaces around +.'); return
    try:
        r=_reaction_thermo_from_sessions(message.chat.id,message.from_user.id,equation)
        lines=[f"Reaction: {r['equation']}"]
        for k,label in [('delta_e_kj_mol','Delta Eel'),('delta_e0_kj_mol','Delta(Eel+ZPE)'),('delta_h_kj_mol','Delta H'),('delta_g_kj_mol','Delta G')]:
            lines.append(f"{label}: {r[k]:.3f} kJ mol^-1" if r.get(k) is not None else f'{label}: unavailable')
        lines.append(f"Delta S: {r['delta_s_j_mol_k']:.3f} J mol^-1 K^-1" if r.get('delta_s_j_mol_k') is not None else 'Delta S: unavailable')
        if r.get('temperature_K') is not None: lines.append(f"T: {r['temperature_K']:.2f} K")
        if r.get('keq') is not None: lines.append(f"K_eq: {r['keq']:.6e}")
        elif r.get('log10_keq') is not None: lines.append(f"log10(K_eq): {r['log10_keq']:.6g}")
        if r.get('pressure_atm') is not None: lines.append(f"Reference pressure: {r['pressure_atm']:.6g} atm")
        if r.get('warnings'):
            lines.append('Warnings:')
            lines.extend('• '+w for w in r['warnings'])
        lines.append('Use mutually consistent levels of theory and thermochemical conditions across all species.')
        bot.reply_to(message,'\n'.join(lines))
    except Exception as exc: bot.reply_to(message,'Reaction thermodynamics error: '+str(exc))


@bot.message_handler(commands=['start'])
@_ordered_context_handler
def start(message):
    if not authorized(message): return
    uid=message.from_user.id
    context_key=_context_key(message)
    with state_lock:
        user_aux_storage.pop(context_key,None)
        user_drive_links.pop(context_key,None)
    bot.reply_to(message,
        "🧪 Computational Chemistry Bot v7.1.2\n"
        f"• Kaggle authentication mode: {_auth_mode_summary()}\n\n"
        "• Send ORCA .inp, Psi4 .dat, or a GaussianView .gjf/.com/.gau file to run on private Kaggle jobs.\n"
        "• The private Kaggle run link arrives as soon as Kaggle accepts the job; use /jobs to retrieve recent links.\n"
        "• Jobs run in /kaggle/working; engine packages stay under /tmp. Working disk is monitored at 20,000 MB.\n"
        "• Send ORCA/Psi4 .out or Gaussian .log for checked analysis. Publication exports include PNG 600 dpi, SVG/PDF and CSV.\n"
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
        'ChemBot build: v7.1.2-FINAL-20261013\n'
        f'Kaggle username configured: {"yes" if bool(KAGGLE_USERNAME) else "no"}\n'
        f'Authentication mode: {_auth_mode_summary()}\n'
        f'Legacy kaggle.json prepared: {"yes" if bool(KAGGLE_AUTH_INFO.get("legacy_key")) else "no"}\n'
        f'API access_token prepared: {"yes" if bool(KAGGLE_AUTH_INFO.get("modern_token")) else "no"}\n'
        f'Kaggle credentials configured (not an API permission test): {"OK" if KAGGLE_STARTUP_AUTH_OK else "FAILED"}\n'
        f'ORCA dataset: {ORCA_DATASET_SLUG}\n'
        f'Gaussian dataset: {GAUSSIAN_DATASET_SLUG}'
    )


@bot.message_handler(commands=['help'])
def help_command(message):
    if not authorized(message): return
    bot.reply_to(message,
        "Supported inputs: ORCA .inp; Psi4 .dat; Gaussian .gjf/.com/.gau or a Gaussian route-card .inp.\n"
        "Auxiliary/restart inputs: .xyz, .allxyz, .gbw, .chk, .fchk.\n"
        "Analysis input: ORCA/Psi4 .out or Gaussian .log; common text formats are validated before reporting.\n"
        "The private Kaggle link is sent immediately after submission; /jobs retrieves recent links.\n"
        "Kaggle layout: engine packages in /tmp; calculations/results in /kaggle/working; disk guard 20,000 MB.\n"
        "Use /clearaux to discard stored auxiliary/restart context after a batch.\n\n"
        "For .out files the bot extracts every recognized section and exposes interactive menus for energies, thermochemistry, frequencies/IR, TD-DFT/UV-Vis, orbital energies, optimization profile and diagnostics. Upload two or more compatible outputs to overlay TD-DFT/UV-Vis or FT-IR spectra. A PDF with generated figures is also produced."
    )


@bot.message_handler(commands=['clearaux'])
@_ordered_context_handler
def clear_aux(message):
    if not authorized(message): return
    uid = message.from_user.id
    context_key = _context_key(message)
    with state_lock:
        n = len(user_aux_storage.get(context_key, {}))
        had_link = bool(user_drive_links.get(context_key))
        user_aux_storage.pop(context_key, None)
        user_drive_links.pop(context_key, None)
    bot.reply_to(message, f"🧹 Cleared {n} auxiliary file(s)" + (" and the restart URL." if had_link else "."))


@bot.message_handler(func=lambda m: bool(m.text and m.text.startswith(('http://','https://'))))
@_ordered_context_handler
def handle_link(message):
    if not authorized(message): return
    if not (message.text or '').strip().lower().startswith('https://'):
        bot.reply_to(message,'Restart archive URLs must use HTTPS.'); return
    with state_lock:
        user_drive_links[_context_key(message)]=message.text.strip()
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
        if not _callback_owner_allowed(call, sess):
            bot.answer_callback_query(call.id, 'Only the analysis owner or an authorized administrator can use this menu.', show_alert=True); return
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
                    with open(p,'rb') as f: bot.send_document(call.message.chat.id,f,caption='Optimization energy profile')
            elif action=='plots':
                for name,p in sess['plots'].items():
                    with open(p,'rb') as f: bot.send_document(call.message.chat.id,f,caption=name.replace('_',' ').title())
            elif action=='pdf':
                with open(sess['pdf'],'rb') as f: bot.send_document(call.message.chat.id,f,caption='Complete computational chemistry analysis report')
            elif action=='pub': _send_publication_exports(call.message.chat.id, sess)
            elif action=='pubfiles': _send_publication_exports(call.message.chat.id, sess, individual=True)
            elif action=='compare': bot.send_message(call.message.chat.id,'Choose overlay type. The latest compatible .out analyses (up to 10) are used:',reply_markup=comparison_menu(sid))
        elif kind=='t': split_send(call.message.chat.id,section_thermo(a,action),'🌡 Thermochemistry')
        elif kind=='s':
            if action=='freq': split_send(call.message.chat.id,section_vibrations(a),'〰️ Vibrational frequencies')
            elif action=='irplot':
                p=sess['plots'].get('ir')
                if p:
                    with open(p,'rb') as f: bot.send_document(call.message.chat.id,f,caption='Calculated IR spectrum')
                else: bot.send_message(call.message.chat.id,'IR intensities were not recognized in this output.')
        elif kind=='c':
            _send_comparison(call.message.chat.id,sess,action)
        elif kind=='u':
            if action=='states': split_send(call.message.chat.id,section_uv(a),'🌈 TD-DFT / UV-Vis states')
            elif action=='uvplot':
                p=sess['plots'].get('uvvis')
                if p:
                    with open(p,'rb') as f: bot.send_document(call.message.chat.id,f,caption='Simulated UV-Vis spectrum')
                else: bot.send_message(call.message.chat.id,'No UV-Vis plot is available.')
    except Exception as e:
        error_id=uuid.uuid4().hex[:10]
        print('Callback failure reference',error_id,':',_redact_kaggle_error(e))
        try: bot.answer_callback_query(call.id,'Could not complete this action. Please retry.',show_alert=True)
        except Exception: pass


@bot.message_handler(commands=['export', 'publication'])
@_ordered_context_handler
def publication_command(message):
    if not authorized(message):
        return
    requested = (message.text or '').partition(' ')[2].strip().casefold()
    with session_lock:
        sessions = [row for row in analysis_sessions.values()
                    if row.get('chat_id') == message.chat.id and row.get('user_id') == message.from_user.id]
    if requested:
        sessions = [row for row in sessions
                    if Path(row['analysis'].get('filename', '')).stem.casefold() == requested]
    if not sessions:
        bot.reply_to(message, 'Upload an ORCA/Psi4 .out or Gaussian .log file first. Use /export [filename stem] to download publication files.')
        return
    sess = max(sessions, key=lambda row: row.get('created', 0))
    try:
        _send_publication_exports(message.chat.id, sess)
    except Exception as exc:
        reference = uuid.uuid4().hex[:10]
        print('Publication export failed', reference, type(exc).__name__)
        bot.reply_to(message, 'Could not send publication exports. Please retry. Reference: ' + reference)

@bot.message_handler(content_types=['document'])
@_ordered_context_handler
def handle_document(message):
    if not authorized(message): return
    uid=message.from_user.id; chat_id=message.chat.id
    context_key=(chat_id,uid)
    original_name=message.document.file_name or 'file'
    filename=original_name.lower()
    size=message.document.file_size or 0

    # Direct scientific analysis of ORCA/Psi4/Gaussian output
    if filename.endswith(('.out','.log')):
        if size>MAX_TEXT_OUT:
            bot.reply_to(message,'The .out file exceeds the current 20 MB direct-analysis safety limit.'); return
        try:
            info=bot.get_file(message.document.file_id)
            raw=bot.download_file(info.file_path)
            if len(raw) > MAX_TEXT_OUT:
                raise ValueError('Downloaded text exceeds the direct-analysis size limit.')
            text=decode_job_input(raw)
            detected_engine=detect_engine(text)
            if detected_engine not in ('ORCA','Psi4','Gaussian'):
                bot.reply_to(message,'❌ This .out file could not be identified as ORCA, Psi4 or Gaussian output. Please send the original plain-text .out file without conversion or truncation.')
                return
            bot.reply_to(message,f'🔬 {detected_engine} output detected. Parsing scientific results and generating figures/PDF...')
            sid,a=create_analysis_session(chat_id,uid,original_name,text,source_bytes=raw)
            _queue_analysis_group(message,sid)
            bot.send_message(chat_id,section_summary(a),reply_markup=out_menu(sid,a))
        except Exception as e:
            error_id=uuid.uuid4().hex[:10]
            print('Direct analysis error', error_id, _redact_kaggle_error(e))
            bot.reply_to(message,'Analysis failed while processing this output. Please retry or resend it. Reference: '+error_id)
        return

    # Auxiliary job files
    if filename.endswith(('.xyz','.allxyz','.gbw','.chk','.fchk')):
        if size>MAX_TELEGRAM_DOWNLOAD:
            bot.reply_to(message,'This auxiliary file exceeds Telegram Bot API 20 MB download limit. Use a restart ZIP URL instead.'); return
        try:
            info=bot.get_file(message.document.file_id); raw=bot.download_file(info.file_path)
            if len(raw) > MAX_TELEGRAM_DOWNLOAD:
                raise ValueError('Downloaded attachment exceeds the auxiliary size limit.')
            with state_lock:
                bucket=user_aux_storage.setdefault(context_key,{})
                aux_name=os.path.basename(original_name.replace('\\','/'))
                replaced= len(base64.b64decode(bucket.get(aux_name, '')))
                projected=sum(len(base64.b64decode(v)) for v in bucket.values())-replaced+len(raw)
                if projected>MAX_AUX_STORAGE:
                    too_large=True
                    current_files=list(bucket)
                else:
                    too_large=False
                    bucket[aux_name]=base64.b64encode(raw).decode('ascii')
                    current_files=list(bucket)
            if too_large:
                bot.reply_to(message,'Auxiliary in-memory files would exceed 20 MB. Send a restart ZIP URL for larger data.'); return
            bot.reply_to(message,f"✅ Stored {original_name}. Current auxiliary files: {', '.join(current_files)}. They remain available to every submitted job until /clearaux or /start.")
        except Exception as e: bot.reply_to(message,'Upload error: '+_redact_kaggle_error(e))
        return

    # Calculation jobs
    if filename.endswith(('.inp','.dat','.gjf','.com','.gau')):
        if size>MAX_TELEGRAM_DOWNLOAD:
            bot.reply_to(message,'Input exceeds Telegram Bot API download limit.'); return
        try:
            info=bot.get_file(message.document.file_id); raw=bot.download_file(info.file_path); inp=decode_job_input(raw)
            if len(raw) > MAX_TELEGRAM_DOWNLOAD:
                raise ValueError('Downloaded input exceeds the calculation-input size limit.')
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
            aux_snapshot, drive = snapshot_user_job_context(chat_id,uid)

            # End-point geometries must be explicit in the scientific input.
            # Keep the received attachment bytes and the normalized execution text.
            input_basename = os.path.basename(original_name.replace('\\', '/'))
            if input_basename in aux_snapshot:
                raise ValueError('The main calculation input collides with an auxiliary filename.')
            payload={input_basename:base64.b64encode(raw).decode('ascii')}
            payload.update(aux_snapshot)
            encoded=base64.b64encode(json.dumps(payload).encode()).decode('ascii')
            # submit_kaggle_job uses a UUID slug, a private tempfile directory,
            # a fresh KaggleApi client, and a finally-cleanup for this job only.
            job_id,job_url=submit_kaggle_job(
                original_name, encoded, chat_id, is_psi4, extras, drive,
                is_gaussian=is_gaussian, prepared_input_text=inp
            )
            with state_lock:
                links=recent_kaggle_jobs.setdefault((chat_id,uid),[])
                links.insert(0,job_url)
                del links[10:]
            notice=(f"✅ Private Kaggle {'notebook' if is_gaussian else 'job'} accepted for {prog}: {original_name}\n"
                    f"🔗 {job_url}\n"
                    "The notebook may still be installing packages or extracting the program before the calculation starts. "
                    "Send /jobs to retrieve recent job links.")
            _send_submission_link(chat_id,notice)
        except Exception as e:
            bot.reply_to(message,'Submission error: '+_redact_kaggle_error(e))
        return

    bot.reply_to(message,'Supported files: ORCA .inp, Psi4 .dat, Gaussian .gjf/.com/.gau, .out/.log, .xyz, .allxyz, .gbw, .chk, .fchk')


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
    if mode != 'auto':
        raise RuntimeError('CHEMBOT_MODE must be auto, webhook, or polling.')
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
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', configured):
            raise RuntimeError('CHEMBOT_WEBHOOK_SECRET must use 1-256 letters, digits, underscores or hyphens.')
        return configured
    # Stable across overlapping Render instances, but never printed or exposed.
    return hashlib.sha256((BOT_TOKEN + "|ChemBot|Webhook|v4.7").encode("utf-8")).hexdigest()[:48]


def _webhook_path():
    configured = os.environ.get("CHEMBOT_WEBHOOK_PATH", "").strip().strip("/")
    if configured:
        if not re.fullmatch(r'[A-Za-z0-9_/-]+', configured) or configured in {'health', 'healthz'}:
            raise RuntimeError('CHEMBOT_WEBHOOK_PATH contains unsupported characters or a reserved health route.')
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
    parsed_public_url = urlsplit(external_url)
    if (parsed_public_url.scheme != 'https' or not parsed_public_url.hostname or parsed_public_url.username
            or parsed_public_url.query or parsed_public_url.fragment):
        raise RuntimeError('The public webhook base URL must be an HTTPS URL without credentials, query or fragment.')

    secret = _webhook_secret()
    path = _webhook_path()
    webhook_url = external_url + path
    port = int(os.environ.get("PORT", "10000"))
    max_update_bytes = int(os.environ.get("CHEMBOT_MAX_WEBHOOK_BYTES", str(2 * 1024 * 1024)))

    class TelegramWebhookHandler(BaseHTTPRequestHandler):
        server_version = "ChemBotWebhook/7.1.2"

        def log_message(self, fmt, *args):
            # BaseHTTPRequestHandler includes the secret route in request logs.
            return

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
                    "version": "7.1.2",
                    "transport": "webhook",
                }).encode("utf-8")
                return self._reply(200, payload, "application/json; charset=utf-8")
            return self._reply(404, b"Not Found")

        def do_POST(self):
            if self.path != path:
                return self._reply(404, b"Not Found")

            header_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(header_secret, secret):
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
                if update is None or not isinstance(update.update_id, int):
                    return self._reply(400, b'Invalid Update')
                # TeleBot is configured threaded=True; handlers are delegated to
                # its worker pool, allowing the HTTP request to acknowledge fast.
                bot.process_new_updates([update])
            except Exception as exc:
                print('Webhook update processing error:', _redact_kaggle_error(exc))
                return self._reply(500, b"Update processing failed")

            return self._reply(200, b"OK")

    server = ThreadingHTTPServer(("0.0.0.0", port), TelegramWebhookHandler)
    server.daemon_threads = True

    # setWebhook replaces any previous webhook atomically. Both the old and new
    # Render instance use the same URL/secret during zero-downtime deployment,
    # so there is no getUpdates race and no need to delete the webhook first.
    ok = bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        drop_pending_updates=False,
        max_connections=1,
        allowed_updates=['message', 'callback_query'],
    )
    if not ok:
        server.server_close()
        raise RuntimeError("Telegram setWebhook returned false.")

    print(f"CHEMBOT v7.1.2 webhook mode active on port {port}.")
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

    print("CHEMBOT v7.1.2 polling mode active. Ensure no other instance uses this bot token.")
    try:
        bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30)
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
