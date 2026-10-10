#!/usr/bin/env bash
# Copy the shared core into the skill and the extension, then package both.
set -euo pipefail
cd "$(dirname "$0")"
SK=plugins/code-flow/skills/code-flow
mkdir -p $SK/scripts $SK/reference vscode-extension/media vscode-extension/python dist
cp core/codeflow_parse.py core/codeflow_render.py core/viewer.html $SK/scripts/
cp core/explain_prompt.md $SK/reference/
cp core/viewer.html core/explain_prompt.md vscode-extension/media/
cp core/codeflow_parse.py vscode-extension/python/
(cd $SK/.. && rm -f ../../../dist/code-flow-skill.zip && zip -qr ../../../dist/code-flow-skill.zip code-flow -x '*/__pycache__/*')
(cd vscode-extension && npx --yes @vscode/vsce package --allow-missing-repository --out ../dist/code-flow-0.7.1.vsix)
echo "built: dist/"
