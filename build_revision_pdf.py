#!/usr/bin/env python3
"""Build response + red revised manuscript. Requires TeX Live and poppler-utils.

python3 build_revision_pdf.py --refresh  # snapshot HEAD and Git index, then build
python3 build_revision_pdf.py            # rebuild from the saved snapshots
python3 build_revision_pdf.py --refresh-response  # use the current response file

Sources and build logs live in revision_package/. Original repositories are
read only. Deleted wording is omitted; additions are red. Changed image
captions are red because binary image changes cannot be text-diffed.
Bibliography markup is suppressed: the user confirmed 34 submitted references;
the 31-entry HEAD .bbl is a stale build artifact, not a reliable baseline.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / 'revision_package'
REPO = Path('/home/sure/ADAPT')


def run(args, cwd=None, input=None):
    return subprocess.run(args, cwd=cwd, input=input, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout


def git(*args, repo=REPO):
    return run(['git', '-C', str(repo), *args])


def needed(name):
    return (name in ('main.tex', 'main.bbl', 'main.bib') or
            name.startswith(('sec/', 'figure/', 'ieeeconf/', 'IEEEtranBST/'))
            and Path(name).suffix in ('.tex', '.cls', '.sty', '.bst', '.bib', '.png', '.jpg', '.pdf'))


def snapshot():
    PACKAGE.mkdir(exist_ok=True)
    old = {}
    for entry in git('ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if entry:
            info, name = entry.split(b'\t', 1)
            mode, kind, oid = info.decode().split()
            if kind == 'blob' and needed(name.decode()):
                old[name.decode()] = oid
    new = {}
    for entry in git('ls-files', '--stage', '-z').split(b'\0'):
        if entry:
            info, name = entry.split(b'\t', 1)
            mode, oid, stage = info.decode().split()
            if stage != '0':
                raise RuntimeError('Unmerged index entry: ' + name.decode())
            if needed(name.decode()):
                new[name.decode()] = oid
    for folder, entries in [('submitted', old), ('revised', new)]:
        dest = PACKAGE / folder
        if dest.exists():
            shutil.rmtree(dest)
        for name, oid in entries.items():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('cat-file', 'blob', oid))
    response = PACKAGE / 'response'
    response.mkdir(exist_ok=True)
    response.joinpath('main.tex').write_bytes(
        git('show', ':修改说明/main.tex', repo=ROOT))
    metadata = {
        'baseline_commit': git('rev-parse', 'HEAD').decode().strip(),
        'revised_source': 'Git index (staged files)',
        'response_source': 'paper_todo Git index: 修改说明/main.tex',
        'changed_figures': [n for n in new if n.startswith('figure/') and old.get(n) != new[n]],
        'submitted_blobs': old, 'revised_blobs': new,
        'marking': 'Added/replaced text and changed-image captions red; deleted text omitted',
        'order': ['response', 'revised manuscript with red changes'],
    }
    (PACKAGE / 'manifest.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')


def prepare_diff():
    # Inline the versioned bibliography to prevent silent regeneration.
    # Bibliography diff markup is removed below because HEAD's .bbl is stale.
    for folder in ('submitted', 'revised'):
        base = PACKAGE / folder
        refs = (base / 'sec/6_references.tex').read_text()
        refs = re.sub(r'\\bibliographystyle\{[^}]*\}', '', refs)
        refs = re.sub(r'\\bibliography\{[^}]*\}', lambda m: r'\input{main.bbl}', refs)
        (base / 'sec/references_for_diff.tex').write_text(refs)
        main = (base / 'main.tex').read_text().replace(
            r'\input{sec/6_references}', r'\input{sec/references_for_diff}')
        (base / 'main_for_diff.tex').write_text(main)
    diff = run(['latexdiff', '--flatten', '--type=CFONT', '--graphics-markup=none',
                '--math-markup=whole', str(PACKAGE / 'submitted/main_for_diff.tex'),
                str(PACKAGE / 'revised/main_for_diff.tex')], cwd=PACKAGE / 'revised').decode()
    (PACKAGE / 'full_diff.tex').write_text(diff)
    preamble, body = diff.split(r'\begin{document}', 1)
    # Preserve additions under a separate command while latexrevise correctly
    # removes deleted blocks, equations, labels, and deleted environments.
    body = re.sub(r'\\DIFadd(?:FL)?\b', lambda m: r'\RevisionAdd', body)
    marked = run(['latexrevise', '--accept'], input=(preamble + r'\begin{document}' + body).encode()).decode()
    macros = '\n' + r'\DeclareRobustCommand{\RevisionAdd}[1]{{\color{red}#1}}' + '\n'
    marked = marked.replace(r'\begin{document}', macros + r'\begin{document}', 1)
    changed = json.loads((PACKAGE / 'manifest.json').read_text())['changed_figures']
    def color_caption(match):
        block = match.group()
        if any('{' + name + '}' in block for name in changed):
            block = re.sub(r'(\\begin\{figure\*?\}(?:\[[^]]*\])?)',
                           lambda m: m.group() + '\n' + r'\captionsetup{textfont={color=red},labelfont={color=red}}',
                           block, count=1)
        return block
    marked = re.sub(r'\\begin\{figure\*?\}.*?\\end\{figure\*?\}', color_caption, marked, flags=re.S)
    # The user confirmed that the submission already contained all 34 entries.
    # Keep the revised bibliography intact, without changes inferred from the
    # stale 31-entry HEAD build artifact. Do not alter the archived HEAD source.
    new_bbl = (PACKAGE / 'revised/main.bbl').read_text()
    bibliography = re.search(r'\\begin\{thebibliography\}.*?\\end\{thebibliography\}', new_bbl, re.S).group()
    marked, count = re.subn(r'\\begin\{thebibliography\}.*?\\end\{thebibliography\}',
                            lambda m: bibliography, marked, flags=re.S)
    if count != 1:
        raise RuntimeError('Expected exactly one bibliography')
    (PACKAGE / 'revised/manuscript_red.tex').write_text(marked)


def compile_tex(folder, name):
    for iteration in range(3):
        result = subprocess.run(['pdflatex', '-interaction=nonstopmode', '-halt-on-error',
                                 '-file-line-error', name + '.tex'], cwd=folder,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (folder / (name + '.build.log')).write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError(result.stdout.decode(errors='replace')[-6000:])
    log = (folder / (name + '.log')).read_text(errors='replace')
    if 'undefined references' in log or re.search(r'Citation .* undefined', log):
        raise RuntimeError('Unresolved references: ' + str(folder / (name + '.log')))
    return folder / (name + '.pdf')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--refresh-response', action='store_true',
                        help='Copy the current working response into the saved package')
    args = parser.parse_args()
    if args.refresh or not (PACKAGE / 'manifest.json').exists():
        snapshot()
    if args.refresh_response:
        shutil.copyfile(ROOT / '修改说明/main.tex', PACKAGE / 'response/main.tex')
        manifest_path = PACKAGE / 'manifest.json'
        metadata = json.loads(manifest_path.read_text())
        metadata['response_source'] = 'Working file: 修改说明/main.tex'
        manifest_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    manifest_path = PACKAGE / 'manifest.json'
    metadata = json.loads(manifest_path.read_text())
    metadata['bibliography_marking'] = (
        'None: user confirmed 34 references in submission; HEAD .bbl with 31 '
        'entries is a stale artifact and is not used to infer reference additions.')
    manifest_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    prepare_diff()
    response = compile_tex(PACKAGE / 'response', 'main')
    manuscript = compile_tex(PACKAGE / 'revised', 'manuscript_red')
    output = ROOT / 'ADAPT_Response_and_Revised_Manuscript.pdf'
    if output.exists():
        output.unlink()
    run(['pdfunite', str(response), str(manuscript), str(output)])
    print(output)
    print(run(['pdfinfo', str(output)]).decode())


if __name__ == '__main__':
    main()
