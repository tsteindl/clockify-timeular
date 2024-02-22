"""
A simple linkage between a Timular cube and Hackaru.
"""

import asyncio
import http.cookiejar
import logging
import os
import signal
import tkinter as tk
from datetime import datetime
from functools import partial
from getpass import getpass
from threading import Lock
from tkinter import simpledialog
from typing import Optional

import appdirs  # type: ignore
import requests
import yamale  # type: ignore
import yaml
from bleak import BleakClient  # type: ignore
from recordclass import RecordClass  # type: ignore
from requests import Session
from tenacity import retry  # type: ignore

MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
MANUFACTURER_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
SOFTWARE_REVISION_UUID = "00002a28-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
ORIENTATION_UUID = "c7e70012-c847-11e6-8175-8c89a55d403c"

HEADERS = {
    "content-type": "application/json",
    "x-requested-with": "XMLHttpRequest",
}

CONFIG_SCHEMA = yamale.make_schema(
    content="""
cli: bool(required=False)
timeular:
    device-address: regex('([0-9A-F]{2}):([0-9A-F]{2}):([0-9A-F]{2}):([0-9A-F]{2}):([0-9A-F]{2}):([0-9A-F]{2})')

clockify:
    endpoint: str()
    email: str()
    api-key: str()

mapping: list(include('task-mapping'))

---

clockify-task:
    name: str(required=False)
    id: int()
    description: str(required=False)

task-mapping:
    side: int(min=1, max=9)
    task: include('clockify-task')
"""
)


logging.basicConfig()
logger = logging.getLogger("clockify_timular")
logger.setLevel(logging.INFO)


state_lock = Lock()


class State(RecordClass):
    """Application state"""

    # pylint: disable=too-few-public-methods
    current_task: Optional[dict]
    config: dict
    session: Session


class GracefulKiller:
    kill_now = False

    def __init__(self, state: State):
        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, partial(self.exit_gracefully, state))

    def exit_gracefully(self, state, *_):
        """ "Stop the current task before exit"""
        stop_current_task(state)
        logger.info("Stopped current task, shutting down.")
        self.kill_now = True


def now():
    """Returns the current time as a formatted string"""
    return datetime.utcnow().strftime("%a %B %d %Y %H:%M:%S")


@retry
def login(session, config):
    """Login to Hackaru Server"""
    data = f'{{"user":{{"email":"{config["email"]}","password":"{getpass()}"}}}}'
    response = session.post(
        config["hackaru"]["endpoint"] + "/auth/auth_tokens",
        data=data,
        headers=HEADERS,
    )

    response.raise_for_status()
    session.cookies.save()


def prompt_for_password(cli: bool):
    if cli:
        return getpass()
    else:
        root = tk.Tk()
        root.overrideredirect(1)
        root.withdraw()

        return (
            simpledialog.askstring(
                title="Task Description", prompt="What are you working on?", show="*"
            )
            or ""
        )


def callback_with_state(
    state: State, sender: int, data: bytearray  # pylint: disable=unused-argument
):
    """Callback for orientation changes of the Timeular cube"""
    assert len(data) == 1
    orientation = data[0]
    logger.info("Orientation: %i", orientation)

    with state_lock:
        if orientation not in range(1, 9):
            stop_current_task(state)
            return

        stop_current_task(state)
        try:
            task = get_task(state, orientation)
            start_task(state, **task)
        except StopIteration:
            logger.error("There is no task assigned for side %i", orientation)


def get_task(state: State, orientation: int):
    """Retrieve a task for an orientation from the config file"""
    task = next(
        mapping["task"]
        for mapping in state.config["mapping"]
        if mapping["side"] == orientation
    )

    return {
        "project_id": task["id"],
        "description": task.get("description", ""),
    }


def start_task(state: State, project_id: int, description: str):
    """Start a task in Hackaru"""
    data = f'{{"activity":{{"description":"{description or prompt_for_description(state.config["cli"])}","project_id":{project_id},"started_at":"{now()}"}}}}'

    resp = state.session.post(state.config["task_endpoint"], data=data, headers=HEADERS)

    state.current_task = resp.json()


def prompt_for_description(cli: bool):
    if cli:
        return input("What are you working on? ")
    else:
        root = tk.Tk()
        root.overrideredirect(1)
        root.withdraw()

        return (
            simpledialog.askstring(
                title="Task Description", prompt="What are you working on?"
            )
            or ""
        )


def stop_current_task(state: State):
    """Stop a task in Hackaru"""
    if state.current_task is None:
        return

    data = f'{{"activity":{{"id":{state.current_task["id"]},"stopped_at":"{now()}"}}}}'

    state.session.put(
        state.config["task_endpoint"] + "/" + str(state.current_task["id"]),
        data=data,
        headers=HEADERS,
    )

    state.current_task = None


async def print_device_information(client):
    """Print device information about the connected Timular cube"""

    model_number = await client.read_gatt_char(MODEL_NUMBER_UUID)
    logger.info("Model Number: %s", "".join(map(chr, model_number)))

    manufacturer = await client.read_gatt_char(MANUFACTURER_UUID)
    logger.info("Manufacturer: %s", "".join(map(chr, manufacturer)))

    serial_number = await client.read_gatt_char(SERIAL_NUMBER_UUID)
    logger.info("Serial Number: %s", "".join(map(chr, serial_number)))

    hardware_revision = await client.read_gatt_char(HARDWARE_REVISION_UUID)
    logger.info("Hardware Revision: %s", "".join(map(chr, hardware_revision)))

    software_revision = await client.read_gatt_char(SOFTWARE_REVISION_UUID)
    logger.info("Software Revision: %s", "".join(map(chr, software_revision)))

    firmware_revision = await client.read_gatt_char(FIRMWARE_REVISION_UUID)
    logger.info("Firmware Revision: %s", "".join(map(chr, firmware_revision)))


async def main_loop(state: State, killer: GracefulKiller):
    """Main loop listening for orientation changes"""
    
    async with BleakClient(state.config["timeular"]["device-address"]) as client:
        print("hey from main loop")

        await print_device_information(client)

        callback = partial(callback_with_state, state)

        await client.start_notify(ORIENTATION_UUID, callback)

        while not killer.kill_now:
            await asyncio.sleep(1)


def main():
    """Console script entry point"""
    config_dir = appdirs.user_config_dir(appname="clockify-timeular")

    with open(
        os.path.join(config_dir, "config.yml"), "r", encoding="utf-8"
    ) as config_file:
        config = yaml.safe_load(config_file)

        data = yamale.make_data(config_file.name)
        yamale.validate(CONFIG_SCHEMA, data)

        #config["task_endpoint"] = config["clockify"]["endpoint"] + "/v1/activities"

        if "cli" not in config:
            config["cli"] = False

        #cookies_file = os.path.join(config_dir, "cookies.txt")
        session = requests.Session()

        HEADERS['x-api-key'] = config["clockify"]["api-key"]
        """
        session.cookies = http.cookiejar.LWPCookieJar(filename=cookies_file)
        try:
            session.cookies.load(ignore_discard=True)
            session.cookies.clear_expired_cookies()
        except FileNotFoundError:
            print("cookie file was not found: ", cookies_file)
            pass

        if not session.cookies:
            login(session, config)
        """
        user_data = session.get(config["clockify"]["endpoint"] + '/user', headers=HEADERS).json()

        "https://api.clockify.me/api/v1/workspaces/{workspaceId}/user/{userId}/time-entries"

        current_task = session.get(
            config["clockify"]["endpoint"] + f"/workspaces/{user_data['activeWorkspace']}/user/{user_data['id']}/time-entries", headers=HEADERS
        ).json()
        
        print(current_task)

        state = State(config=config, current_task=current_task, session=session)
        killer = GracefulKiller(state)

        asyncio.run(main_loop(state, killer))
        """
        data = {'x-api-key': config["clockify"]["api-key"]}
        r = session.get(config["clockify"]["endpoint"] + '/user', headers=data)
        print(r.content)
        """
