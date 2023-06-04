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

DEFAULT_CALIB_SLOPE = 1.0
DEFAULT_CALIB_OFFSET = 0.0
DEFAULT_DIST_LOW = 10
DEFAULT_DIST_HIGH = 100

# Create a shared queue for the measurement data
data_queue = queue.Queue()

def parse_args():
    parser = argparse.ArgumentParser(description="Rearview radar logger")
    parser.add_argument("--print_stream", action="store_true", help="Print live data stream to console")
    parser.add_argument("--write_stream", action="store_true", help="Write live data stream to file")
    parser.add_argument("--sticker_id", type=str, help="Sticker ID of the device to connect to")
    parser.add_argument("--runtime", type=float, default=None, help="Duration to run the application for in seconds, default infinity")
    parser.add_argument("--calib_slope", type=float, default=None, help=f"Calibration slope to apply to data, default {DEFAULT_CALIB_SLOPE}")
    parser.add_argument("--calib_offset", type=float, default=None, help=f"Calibration offset to apply to data, default {DEFAULT_CALIB_OFFSET}")
    parser.add_argument("--dist_low", type=int, default=None, help=f"Distance lower limit in metres, default {DEFAULT_DIST_LOW} metres")
    parser.add_argument("--dist_high", type=int, default=None, help=f"Distance upper limit in metres, default {DEFAULT_DIST_HIGH} metres")

    # Validate arguments
    args = parser.parse_args()
    if args.sticker_id is not None and (not isinstance(args.sticker_id, str) or len(args.sticker_id) != 9 or not args.sticker_id.isalnum()):
        parser.error("Sticker ID must be a 9-character alphanumeric string")
    if args.runtime is not None and args.runtime <= 0:
        parser.error("Runtime must be positive")
    if args.calib_slope is not None and (args.calib_slope > 5 or args.calib_slope < -5):
        parser.error("calib_slope must be between -5 and 5")
    if args.calib_offset is not None and (args.calib_offset > 255 or args.calib_offset < -255):
        parser.error("calib_offset must be between -255 and 255")
    if args.dist_low is not None and (args.dist_low < 0 or args.dist_low > 255):
        parser.error("dist_low must be between 0 and 255")
    if args.dist_high is not None and (args.dist_high < 0 or args.dist_high > 255):
        parser.error("dist_high must be between 0 and 255")
    # Check that dist_high is greater than dist_low, including cases when one or both are None
    if (args.dist_low is not None and args.dist_high is not None and args.dist_high < args.dist_low) or \
        (args.dist_low is None and args.dist_high is not None and args.dist_high < DEFAULT_DIST_LOW) or \
        (args.dist_low is not None and args.dist_high is None and args.dist_low > DEFAULT_DIST_HIGH):
        parser.error("dist_high must be greater than dist_low")

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
    # Find the indices which cover the slowest 95% of speeds
    num_samples_95 = int(len(sorted_speeds) * 0.05)
    max_speed_95 = max(sorted_speeds[num_samples_95:])
    avg_speed = round(sum(speeds) / len(speeds), 1) # round to 1 decimal place
    earliest_timestamp = min([d[0] for d in data])
    return {
        "timestamp": earliest_timestamp,
        "max": max_speed,
        "95% max": max_speed_95,
        "average": avg_speed,
        "data": [(d[1], d[2]) for d in data],
    }

def consume_data_queue(print_stream=False, write_stream=False, calib_slope=None, calib_offset=None, dist_low=None, dist_high=None, sentinel=None):
    # Check for calibration data
    if calib_slope is None or calib_offset is None:
        print("Error: Calibration data not provided")
        return
    # Check for distance thresholds
    if dist_high is None or dist_low is None:
        print("Error: Distance thresholds not provided")
        return

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
                    summary["threat_id"] = threat_id
                    summary["calibration"] = [calib_slope, calib_offset]
                    summary["dist_thresholds"] = [dist_low, dist_high]
                    # Write it to the output file
                    with open(DATA_FILE, "a") as f:
                        json.dump(summary, f)
                        f.write("\n")
                break

            # Check if distance is within thresholds. If not, discard the data
            if entry.distance < dist_low or entry.distance > dist_high:
                continue

            # Apply calibration values, round to 1 decimal place
            calib_speed = round(entry.speed * calib_slope + calib_offset, 1)

            if print_stream:
                print(timestamp + " " + str(entry))
            if write_stream:
                with open(DATA_STREAM_FILE, "a") as f:
                    f.write(timestamp + " " + str(entry) + "\n")

            # Add the data to the dictionary
            if entry.threat_id not in data_dict:
                data_dict[entry.threat_id] = []
            data_dict[entry.threat_id].append((timestamp, calib_speed, entry.distance))

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
                newest_timestamp = data[-1][0]
                # Check if the newest timestamp is more than 10 seconds old
                if time.mktime(time.strptime(timestamp, "%Y-%m-%d %H:%M:%S")) - \
                   time.mktime(time.strptime(newest_timestamp, "%Y-%m-%d %H:%M:%S")) > 10:
                    # Summarize the data
                    summary = calculate_summary(data)
                    summary["threat_id"] = threat_id
                    summary["calibration"] = [calib_slope, calib_offset]
                    summary["dist_thresholds"] = [dist_low, dist_high]
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

    # Use calibration and threshold values from the config file if they exist, otherwise use the defaults
    if args.calib_slope is None:
        if config and config.get("calibration_slope"):
            calib_slope = config["calibration_slope"]
        else:
            calib_slope = DEFAULT_CALIB_SLOPE
    else:
        calib_slope = args.calib_slope
    if args.calib_offset is None:
        if config and config.get("calibration_offset"):
            calib_offset = config["calibration_offset"]
        else:
            calib_offset = DEFAULT_CALIB_OFFSET
    else:
        calib_offset = args.calib_offset
    if args.dist_high is None:
        if config and config.get("dist_threshold_high"):
            dist_high = config["dist_threshold_high"]
        else:
            dist_high = DEFAULT_DIST_HIGH
    else:
        dist_high = args.dist_high
    if args.dist_low is None:
        if config and config.get("dist_threshold_low"):
            dist_low = config["dist_threshold_low"]
        else:
            dist_low = DEFAULT_DIST_LOW
    else:
        dist_low = args.dist_low

    # Save device address to config file
    if config is None:
        config = {}
    config["sticker_id"] = sticker_id
    config["bluetooth_address"] = device_address
    config["calibration_slope"] = calib_slope
    config["calibration_offset"] = calib_offset
    config["dist_threshold_low"] = dist_low
    config["dist_threshold_high"] = dist_high
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
        consumer_thread = threading.Thread(target=consume_data_queue, kwargs={"print_stream": args.print_stream, "write_stream": args.write_stream, "calib_slope": calib_slope, "calib_offset": calib_offset, "dist_low": dist_low, "dist_high": dist_high, "sentinel": sentinel})
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