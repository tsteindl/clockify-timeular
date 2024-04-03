# clockify-timeular
Use your timeular cube to track your time with clockify.
Adapated from https://github.com/pSub/hackaru-timeular

## Clockify API documentation
https://docs.clockify.me/


# setuptools
python setup.py clean --all && pip install . && clockify-timeular


# TODO
- async pomodoro tasks should be stopped once orientation has been changed once (maybe with active flag in state)
- pomdoro breaks should be deducted from time
- on fail (eg internet connection) wait and try again
- make so that it automatically runs / or at least is easy to call 

- auto generate files in appdata
- improve user interaction for updating tasks (maybe like in timeular) ("what are you working on")
