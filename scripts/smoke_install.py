"""Build and test wheel and sdist in fresh environments (requires build, twine)."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(*args, cwd):
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Runtime to test")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ddp-smoke-") as directory:
        work = Path(directory)
        dist = work / "dist"
        # Default build creates an sdist, then builds the wheel from that sdist.
        run(sys.executable, "-m", "build", "--outdir", dist, project, cwd=work)
        artifacts = sorted(dist.iterdir())
        if len(artifacts) != 2:
            raise RuntimeError("Expected exactly one wheel and one sdist")
        run(sys.executable, "-m", "twine", "check", "--strict", *artifacts, cwd=work)
        for index, artifact in enumerate(artifacts):
            env = work / f"runtime-{index}"
            run(args.python, "-m", "venv", env, cwd=work)
            python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run(python, "-m", "pip", "install", artifact, cwd=work)
            run(python, "-m", "pip", "check", cwd=work)
            run(python, "-I", "-m", "unittest", "discover", "-v", "-s",
                project / "tests", cwd=work)
            run(python, "-m", "pip", "freeze", cwd=work)
        print("Wheel and sdist clean-install checks passed", flush=True)


if __name__ == "__main__":
    main()
