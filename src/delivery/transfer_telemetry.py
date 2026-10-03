"""Read lightweight task counters without recursively scanning NAS files."""

import base64
import json
import shlex


REMOTE_SAMPLE = r'''
import json, pathlib, time
job = JOB
marker_dir = MARKER_DIR
def processes():
    rows = {}
    for p in pathlib.Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            args = (p/'cmdline').read_bytes().decode().split('\0')
            if not args or pathlib.Path(args[0]).name != 'ossutil': continue
            if not any(job + '.ini' in a for a in args): continue
            if '-r' not in args: continue
            source = args[args.index('-r') + 1]
            name = source.rstrip('/').rsplit('/', 1)[-1]
            io = dict(line.split(':', 1) for line in (p/'io').read_text().splitlines())
            rows[p.name] = (name, int(io['wchar']))
        except (OSError, ValueError, KeyError): continue
    return rows
before = processes(); start = time.monotonic(); time.sleep(3)
after = processes(); elapsed = time.monotonic() - start
written = sum(max(0, v[1] - before[k][1]) for k, v in after.items() if k in before)
completed = sorted(p.stem for p in pathlib.Path(marker_dir).glob('*.done')) if marker_dir else []
print(json.dumps({'active': sorted(set(v[0] for v in after.values())),
    'completed': completed, 'speed_bps': int(written / elapsed),
    'sample_seconds': round(elapsed, 1), 'sampled_at': int(time.time())}))
'''


def sample_command(job, marker_dir=''):
    source = REMOTE_SAMPLE.replace('JOB', repr(job)).replace('MARKER_DIR', repr(marker_dir))
    encoded = base64.b64encode(source.encode()).decode()
    return 'python3 -c ' + shlex.quote(
        'import base64; exec(base64.b64decode(' + repr(encoded) + '))'
    )


def parse_sample(output):
    data = json.loads(output)
    if not isinstance(data, dict):
        raise ValueError('invalid transfer telemetry')
    return data
