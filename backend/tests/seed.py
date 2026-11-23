"""Reset isolated FactoryOS demo data with explicit opt-in: `python seed.py --demo`."""

import argparse
import asyncio

from lib.demo_safety import DemoSeedSafetyError
from lib.seed_data import seed_from_cli


async def main(demo_opt_in: bool = False) -> None:
    if not demo_opt_in:
        raise DemoSeedSafetyError("pass --demo to explicitly opt in to demo seeding")
    await seed_from_cli()
    print("FactoryOS demo data seeded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the isolated FactoryOS demo database.")
    parser.add_argument("--demo", action="store_true", help="explicitly allow demo data seeding")
    args = parser.parse_args()
    asyncio.run(main(demo_opt_in=args.demo))
