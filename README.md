# Radar logger

A project to log radar data from the Garmin Varia series of cycling radars. The application tracks individual vehicles detected by the radar unit and reports the maximum speed, maximum speed after removing the fastest 5% of samples, and average speed values together with all of the reported values for further analysis. Units are reported in metric: km/h for speed and metres for distance. Calibration and threshold values are also included.

This data may be useful to give a first order approximation of whether traffic calming is needed on your local street using inexpensive commodity hardware.

## Installation

1. Clone the repository: `git clone https://github.com/maciejwie/radar-logger.git`
2. Install the required packages: `pip install -r requirements.txt`

## Usage

1. Turn on your Garmin Varia radar and note the ID on the sticker on the back of the unit
2. Run the application: `python main.py --sticker_id STICKER_ID`, specifying what's on the bottom line of the sticker. This information is saved in the `config.json` file so this only needs to be done once, or when changing units.
3. By default, the application will log summaries to both terminal and a `data.json` file. Raw data streaming can be enabled using the `--print_stream` and `--write_stream` parameters. Also by default, the application will run forever until escaped. This can be overridden with the `--runtime` parameter, specified in seconds.


The `config.json` is a convenience configuration file so that parameters not need to be specified every time. The format is simple JSON and contains a few fields:
```
sticker_id: the sticker ID value previously provided at the commandline
bluetooth_address: Bluetooth address last found for the sticker_id
calibration_slope: calibration slope applied to the radar data
calibration_offset: calibration offset applied to the radar data
dist_threshold_low: detection distance threshold lower bound
dist_threshold_high: detection distance threshold upper bound
```
If `--sticker_id` or other parameters whose values are stored in the configuration file are specified, the parameter takes precedence over `config.json`.

## Common Issues

Some issues that I ran into during development:
1) Data from the Varia was not accurate, compared to a reference vehicle. My unit consistently reported values that were about 5 km/h higher than reality. My testing is n=1, so there are `--calib_slope` and `--calib_offset` values to tune it to your unit. My testing also showed that data accuracy fell off below about 8 metres, for large vehicles. The default distance threshold is set to 10-100 metres but can be overridden with `--dist_low` and `--dist_high`.
2) The Varia sensor does not want to pair with my Macbook. This means that after about 5 minutes, it gives up if it can't find anything else to pair with (ex: Garmin headunit over ANT+)
3) Radar disconnected issues. These would most commonly occur when I was relatively far from the sensor and could be do to interference.

For best results, I had a paired a Garmin headunit with the Varia sensor and started recording a ride during data capture. This ride is just to keep the Varia connection alive and can be discarded later. I also recommend setting the Light profile to "Trail" so that it doesn't flash oncoming traffic.

## Contributing

This is a hobby project, but contributions are welcome! Please open an issue or pull request if you would like to contribute.

## License

This project is licensed under the [MIT License](https://opensource.org/licenses/MIT).