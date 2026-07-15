import sys


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in {"--print-provider-env", "--provider-env"}:
        from pi_manager.provider_env import main as provider_env_main

        return provider_env_main(sys.argv[2:])
    from pi_manager.ui import run_app

    return run_app()

if __name__ == "__main__":
    raise SystemExit(main())
