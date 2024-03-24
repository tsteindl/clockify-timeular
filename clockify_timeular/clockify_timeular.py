import asyncio
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
import copy

import appdirs  # type: ignore
import requests
import yamale  # type: ignore
import yaml
from bleak import BleakClient  # type: ignore
from recordclass import RecordClass  # type: ignore
from requests import Session
import time

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
    api-key: str()

mapping: list(include('time_entry-mapping'))

---

clockify-time_entry:
    description: str(required=False)
    project: str(required=False)
    task: str(required=False)

time_entry-mapping:
    side: int(min=1, max=9)
    time_entry: include('clockify-time_entry')
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
    orientation: int
    start_time: str    
    elapsed_time: int
    to_be_changed: bool

    def __eq__(self, other):
        return isinstance(other, State) and self.current_task is not None and other.current_task is not None and self.current_task.id == other.current_task.id

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
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

async def callback_with_state(
    state: State, client: BleakClient, sender: int, data: bytearray  # pylint: disable=unused-argument
):
    """Callback for orientation changes of the Timeular cube"""
    assert len(data) == 1
    orientation = data[0]
    logger.info("Orientation: %i", orientation)
    if orientation not in range(1, 9):
        stop_current_task(state)
        return

    stop_current_task(state)
    with state_lock:
        try:
            print(f"{state.orientation == orientation = }")
            if orientation != state.orientation:
                print("new orientation", orientation)
                state.orientation = orientation
                if not state.to_be_changed:
                    state.elapsed_time = 0
                    state.to_be_changed = True  
                    state.start_time = now()
            
            
            print(f"{state.elapsed_time = }")
            print(f"{state.to_be_changed = }")
            if state.elapsed_time >= 10 and state.to_be_changed:
                time_entry = get_time_entry(state, state.orientation)
                start_time_entry(state, state.start_time, **time_entry)
                state.to_be_changed = False
            else:
                print("need to wait ", 10-state.elapsed_time) #todo remove this
        except StopIteration:
            logger.error("There is no task assigned for side %i", orientation)

def get_time_entry(state: State, orientation: int):
    """Retrieve project (and task) for an orientation from the config file"""
    time_entry = next(
        mapping["time_entry"]
        for mapping in state.config["mapping"]
        if mapping["side"] == orientation
    )

    result = {
        "description": time_entry["description"]
    }

    project = next(filter(lambda project: project["name"] == time_entry["project"], state.config["projects"]), None)

    if project:
        result["project_id"] = project["id"] 
    else:
        data = {
            "name": time_entry["project"]
        }
        resp = state.session.post(
            state.config["clockify"]["endpoint"] + f"/workspaces/{state.config['workspace']}/projects", 
            json=data, 
            headers=HEADERS
        )
        if resp.status_code == 201:
            project = resp.json()
            result["project_id"] = project["id"]
            state.config["projects"].append(project)

    print("time entry:")
    print(time_entry)
    #get task if exists
    if "task" in time_entry:
        if project["id"] not in state.config["tasks"]:
            #on demand requesting of tasks for projects
            resp = state.session.get(
                state.config["clockify"]["endpoint"] + f"/workspaces/{state.config['workspace']}/projects/{project['id']}/tasks", 
                headers=HEADERS
            )
            if resp.status_code == 200:
                state.config["tasks"][project["id"]] = resp.json()
        
        task = next(filter(lambda task: task["name"] == time_entry["task"], state.config["tasks"][project["id"]]), None)
        if task:
            result["task_id"] = task["id"]
        else:
            #create new task
            data = {
                "name": time_entry["task"]
            }
            resp = state.session.post(
                state.config["clockify"]["endpoint"] + f"/workspaces/{state.config['workspace']}/projects/{project['id']}/tasks", 
                json=data,
                headers=HEADERS
            )
            if resp.status_code == 201:
                task = resp.json()
                result["task_id"] = task["id"]
                state.config["tasks"][project["id"]].append(task)

    return result

def start_time_entry(state: State, start_time: str, description: str, project_id: str, task_id: str = None):
    """Start a time entry in Clockify"""
    data = {
        "description": description,
        "start": start_time,
        "projectId": project_id
    }

    if task_id:
        data["taskId"] = task_id

    resp = state.session.post(
        state.config["clockify"]["endpoint"] + f"/workspaces/{state.config['workspace']}/time-entries", 
        json=data, 
        headers=HEADERS
    )
    if resp.status_code == 201:
        state.current_task = resp.json()
        proj = next(filter(lambda project: project['id'] == state.current_task['projectId'], state.config['projects']), None)
        if proj and proj["name"]:
            logger.info(f"Started time entry {state.current_task['description']} from project {proj['name']}")
        else:
            logger.info(f"Started time entry {state.current_task['description']}")

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
    """Stop a task in Clockify"""
    if state.current_task is None:
        return

    data = {"end": now()}

    state.session.patch(
        state.config["clockify"]["endpoint"] + f"/workspaces/{state.config['workspace']}/user/{state.config['user_id']}/time-entries",
        json=data,
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

    while not killer.kill_now:
        try:
            async with BleakClient(state.config["timeular"]["device-address"]) as client:
                await print_device_information(client)

                callback = partial(callback_with_state, state, client)

                await client.start_notify(ORIENTATION_UUID, callback)

                while not killer.kill_now:
                    state.elapsed_time += 1
                    print(f"{state.elapsed_time=}")
                    await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Failed to connect to client: {e}\nRetrying in 5 seconds...")
            await asyncio.sleep(5)

async def update_state_loop(state, config_dir, killer):
    while not killer.kill_now:
        with open(
            os.path.join(config_dir, "config.yml"), "r", encoding="utf-8"
        ) as config_file:
            state.config.update(yaml.safe_load(config_file))

            data = yamale.make_data(config_file.name)
            yamale.validate(CONFIG_SCHEMA, data)
            print("updated state")
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

        if "cli" not in config:
            config["cli"] = False

        session = requests.Session()

        HEADERS['x-api-key'] = config["clockify"]["api-key"]
        user_data = session.get(config["clockify"]["endpoint"] + '/user', headers=HEADERS).json()
        config["workspace"] = user_data['activeWorkspace']
        config["user_id"] = user_data['id']

        time_entries = session.get(
            config["clockify"]["endpoint"] + f"/workspaces/{config['workspace']}/user/{config['user_id']}/time-entries", 
            headers=HEADERS
        ).json()
          
        config["projects"] = session.get(
            config["clockify"]["endpoint"] + f"/workspaces/{config['workspace']}/projects", 
            headers=HEADERS
        ).json()


        config["tasks"] = {}
        
        current_time_entry = next(filter(lambda time_entry: time_entry["timeInterval"]["end"] is None, time_entries), None)

        state = State(config=config, current_task=current_time_entry, session=session, orientation=0, start_time=now(), elapsed_time=0, to_be_changed=False)
        killer = GracefulKiller(state)

        asyncio.run(main_loop(state, killer))
