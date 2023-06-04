import argparse
import asyncio
import json
import time
import queue
import threading
from bleak import BleakClient
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from pycycling.rear_view_radar import RearViewRadarService

CONFIG_FILE = "config.json"
DATA_STREAM_FILE = "data_stream.txt"
DATA_FILE = "data.json"

# Create a shared queue for the measurement data
data_queue = queue.Queue()

def parse_args():
    parser = argparse.ArgumentParser(description="Rearview radar logger")
    parser.add_argument("--print_stream", action="store_true", help="Print live data stream to console")
    parser.add_argument("--write_stream", action="store_true", help="Write live data stream to file")
    parser.add_argument("--sticker_id", type=str, help="Sticker ID of the device to connect to")
    parser.add_argument("--runtime", type=float, default=None, help="Duration to run the application for in seconds, default infinity")

    # Validate arguments
    args = parser.parse_args()
    if args.sticker_id is not None and (not isinstance(args.sticker_id, str) or len(args.sticker_id) != 9 or not args.sticker_id.isalnum()):
        parser.error("Sticker ID must be a 9-character alphanumeric string")
    if args.runtime is not None and args.runtime <= 0:
        parser.error("Runtime must be positive")

    return parser.parse_args()

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, PermissionError):
        return None

def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)
    except PermissionError:
        print("Error: Could not save configuration file - permission denied")

def device_filter_func(device: BLEDevice, adv: AdvertisementData, sticker_id: str) -> bool:
    # Check if last 5 digits of sticker_id are in the advertisement data
    if sticker_id[-5:] in str(adv):
        return True
    return False

async def get_device_address(sticker_id: str) -> str:
    device = await BleakScanner.find_device_by_filter(lambda d, a: device_filter_func(d, a, sticker_id), timeout=10.0)
    print("Found device " + device.name + " with Bluetooth address " + device.address)

    # Save sticker_id and device address to config file
    config = {"sticker_id": sticker_id, "bluetooth_address": device.address}
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)

    return device.address

def calculate_summary(data):
    speeds = [d[1] for d in data]
    sorted_speeds = sorted(speeds, reverse=True)
    max_speed = sorted_speeds[0]
    max_speed_95 = sorted_speeds[int(len(sorted_speeds) * 0.95)]
    avg_speed = sum(speeds) / len(speeds)
    earliest_timestamp = min([d[0] for d in data])
    return {
        "timestamp": earliest_timestamp,
        "max": max_speed,
        "95% max": max_speed_95,
        "average": avg_speed,
        "data": [(d[1], d[2]) for d in data],
    }

def consume_data_queue(print_stream=False, write_stream=False, sentinel=None):
    print("Starting consumer thread")
    # Consume the shared queue and process each entry
    data_dict = {}
    last_timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    while True:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            # Get the next entry from the queue
            entry = data_queue.get(timeout=1.0)
            # Exit when sentinel is received
            if entry is sentinel:
                print("Received sentinel, exiting consumer thread")
                # Flush remaining data
                for threat_id, data in data_dict.items():
                    # Summarize the data
                    summary = calculate_summary(data)
                    # Write it to the output file
                    with open(DATA_FILE, "a") as f:
                        json.dump(summary, f)
                        f.write("\n")
                break

            if print_stream:
                print(timestamp + " " + str(entry))
            if write_stream:
                with open(DATA_STREAM_FILE, "a") as f:
                    f.write(timestamp + " " + str(entry) + "\n")

            # Add the data to the dictionary
            if entry.threat_id not in data_dict:
                data_dict[entry.threat_id] = []
            data_dict[entry.threat_id].append((timestamp, entry.speed, entry.distance))

        except queue.Empty:
            pass
        except KeyboardInterrupt:
            break

        # Once per second, check for any old and stale (last seen >10 seconds ago) data.
        # If found, summarize it, write it to the output file, and remove it from the dictionary.
        if timestamp != last_timestamp:
            last_timestamp = timestamp
            # Operate on a copy because the dictionary will be modified during iteration
            for threat_id, data in data_dict.copy().items():
                newest_timestamp = max([d[0] for d in data])
                # Check if the newest timestamp is more than 10 seconds old
                if time.mktime(time.strptime(timestamp, "%Y-%m-%d %H:%M:%S")) - \
                   time.mktime(time.strptime(newest_timestamp, "%Y-%m-%d %H:%M:%S")) > 10:
                    # Summarize the data
                    summary = calculate_summary(data)
                    # Write it to the output file
                    with open(DATA_FILE, "a") as f:
                        json.dump(summary, f)
                        f.write("\n")
                    # Remove it from the dictionary so that the threat_id can be reused
                    del data_dict[threat_id]

async def main():
    args = parse_args()
    config = load_config()

    if args.sticker_id:
        sticker_id = args.sticker_id
    elif config and config.get("sticker_id"):
        sticker_id = config["sticker_id"]
    else:
        print("Error: --sticker_id parameter or sticker_id and bluetooth_address fields in config.json are required")
        exit(1)

    # Use the bluetooth address from the config file if it exists, otherwise find it using the sticker_id
    # If --sticker_id was specified, use it since it was probably specified for a reason
    if config and config.get("bluetooth_address") and not args.sticker_id:
        device_address = config["bluetooth_address"]
    else:
        device_address = await get_device_address(sticker_id)
    if device_address is None:
        print("Device not found, exiting. Check that the device is on and in range.")
        exit(1)

    # Save device address to config file
    if config is None:
        config = {}
    config["sticker_id"] = sticker_id
    config["bluetooth_address"] = device_address
    save_config(config)

    # Connect to the device and start the radar service
    async with BleakClient(device_address) as client:
        def my_measurement_handler(data):
            # If there's data, send it to the shared queue for processing
            if data:
                # Put each entry into the queue separately
                for entry in data:
                    data_queue.put(entry)

        # Wait until the client is connected before continuing
        while not client.is_connected:
            await asyncio.sleep(0.5)

        # Create a RearViewRadarService object and set the measurement handler
        radar_service = RearViewRadarService(client)
        radar_service.set_radar_measurement_handler(my_measurement_handler)

        # Start a thread to consume the shared queue
        sentinel = object()
        #data_queue.put(sentinel)
        consumer_thread = threading.Thread(target=consume_data_queue, kwargs={"print_stream": args.print_stream, "write_stream": args.write_stream, "sentinel": sentinel})
        consumer_thread.start()
        # Start the radar notification stream
        await radar_service.enable_radar_measurement_notifications()

        # Wait for the specified runtime or until the user stops the program
        if args.runtime:
            await asyncio.sleep(args.runtime)
        else:
            await asyncio.Event().wait()

        # Stop the radar notification stream
        await radar_service.disable_radar_measurement_notifications()
        # Stop the consumer thread
        data_queue.put(sentinel)
        consumer_thread.join()


if __name__ == "__main__":
    asyncio.run(main())