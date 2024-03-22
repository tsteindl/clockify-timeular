            """" Setup """
            from pathlib import Path

            from setuptools import setup

            this_directory = Path(__file__).parent
            long_description = (this_directory / "README.md").read_text()

            setup(
                name="clockify_timeular",
                version="0.1.0",
                description="Track your time with the Timeular cube and Clockify, credits: https://github.com/pSub/hackaru-timeular",
                long_description=long_description,
                long_description_content_type="text/markdown",
                url="https://github.com/tsteindl/clockify-timeular",
                author="Tobias Steindl",
                author_email="tobias.steindl@gmx.net",
                packages=["clockify_timeular"],
                install_requires=[
                    "bleak",
                    "recordclass",
                    "appdirs",
                    "requests",
                    "PyYAML",
                    "tenacity",
                    "yamale",
                ],
                entry_points={
                    "console_scripts": ["clockify-timeular=clockify_timeular:main"],
                },
            )
