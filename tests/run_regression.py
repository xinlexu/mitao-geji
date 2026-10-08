import json
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parent
results = []
for path in sorted(root.glob('test_*.py')):
    test = subprocess.run([sys.executable, '-X', 'utf8', str(path)], cwd=root, text=True, capture_output=True, timeout=120)
    output = test.stdout + test.stderr
    match = re.search(r'Ran (\d+) tests?', output)
    result = {'test': path.name, 'passed': test.returncode == 0 and match is not None, 'count': int(match[1]) if match else 0}
    results.append(result)
    print(json.dumps(result), flush=True)
    if not result['passed']:
        print(output[-12000:], flush=True)
summary = {'suites': results, 'tests': sum(r['count'] for r in results), 'success': all(r['passed'] for r in results)}
print(json.dumps(summary))
raise SystemExit(0 if summary['success'] else 1)
