"""
@file       data_transformer.py - UCSCFarmHaybarnField-20241014_20250130

Convert provided XLSX into GeoJSON files.

@author     jLab
@author     HARE Lab

@version    0.0.9
"""

import argparse
from datetime import datetime, timedelta
import geojson
import json
import numpy as np
import os
import pandas as pd
import re
import sys
import matplotlib.pyplot as plt

# Make the simulation package importable when this file is run directly.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simulation.src.models import Datum, Sensor


"""_ingest_sensor_data_single_dict(data_json) -> Sensor, crs

@param  data_json   (dict)      Data from a sensor saved as a dict.
@return             (Sensor)    Translated data.
@return crs         (str)       Coordinate reference system used (if any).
"""


def _ingest_sensor_data_single_dict(data_json: dict) -> Sensor:
    data = []
    name = data_json["name"]
    # Assume coordinates and depth do not change before the file entry.
    coordinates = data_json["features"][-1]["geometry"]["coordinates"]
    crs = None  # TODO(nubby)
    depth = data_json["features"][-1]["properties"]["depth"]
    [
        data.append(
            Datum(
                timestamp=datetime.strptime(
                    entry["properties"]["ts"], "%Y-%m-%d %H:%M:%S"
                ),
                SC=entry["properties"]["SC"],
                ST=entry["properties"]["ST"],
                VWC=entry["properties"]["WC"],
            )
        )
        for entry in data_json["features"]
    ]
    return (
        Sensor(
            coordinates=coordinates, crs=crs, data=data, depth=depth, layer=0, name=name
        ),
        crs,
    )


"""
"""


def geojson2sensor(path: str):
    print(f"Ingesting sensor data from {path}...")
    with open(path, "r+") as fp:
        try:
            raw_data = geojson.load(fp)
            sensor, crs = _ingest_sensor_data_single_dict(raw_data)
        except Exception as e:
            print(f"{e}: {path}")
            sensor = None
    return sensor


"""_get_xlsx_files_from_dir(path_dir)
"""


def _get_xlsx_files_from_dir(path_dir: str) -> list:
    # Return an empty list if path does not exist.
    if not os.path.isdir(path_dir):
        return []

    return [
        os.path.join(path_dir, f)
        for f in os.listdir(path_dir)
        if f.endswith(".xlsx") and os.path.isfile(os.path.join(path_dir, f))
    ]


"""_convert_xlsx_to_sensors(path)
"""


def _convert_xlsx_to_sensors(path: str) -> list[Sensor]:
    sensors = []

    depth_lut = {0: 0.15, 1: 0.3, 2: 0.9, 3: 0.15, 4: 0.3, 5: 0.9}

    # Get the coordinates of the two sets of sensors based on filename.
    coord_lut = {
        "b1c1": (584134.5112925094, 4093377.3821872333),
        "b1c2": (584146.4315727367, 4093386.8776871837),
        "b1c3": (584158.351852964, 4093396.3731871336),
        "b1c4": (584170.2721331913, 4093405.8686870835),
        "b1c5": (584182.1924134187, 4093415.364187034),
        "b2c1": (584138.3094924893, 4093372.6140751424),
        "b2c2": (584150.2297727166, 4093382.109575093),
        "b2c3": (584162.150052944, 4093391.6050750427),
        "b2c4": (584174.0703331713, 4093401.1005749926),
        "b2c5": (584185.9906133986, 4093410.596074943),
        "b3c1": (584142.1076924694, 4093367.8459630515),
        "b3c2": (584154.0279726967, 4093377.341463002),
        "b3c3": (584165.948252924, 4093386.836962952),
        "b3c4": (584177.8685331513, 4093396.3324629017),
        "b3c5": (584189.7888133787, 4093405.827962852),
        "b4c1": (584145.9058924494, 4093363.0778509607),
        "b4c2": (584157.8261726768, 4093372.573350911),
        "b4c3": (584169.7464529041, 4093382.068850861),
        "b4c4": (584181.6667331314, 4093391.564350811),
        "b4c5": (584193.5870133587, 4093401.059850761),
    }
    basename = os.path.splitext(os.path.basename(path))[0]  # e.g. "logger_B1C1"
    location_labels = basename.split("_", 1)[-1].lower().split("+")

    # Convert the provided Excel file inte DataFrames for reading.
    excel = pd.ExcelFile(path)
    sheet_pattern = r"Processed Data Config*"
    data_sheets = [
        sheet for sheet in excel.sheet_names if re.match(sheet_pattern, sheet)
    ]

    # Read from each data set recorded.
    for sheet in data_sheets:
        df = pd.read_excel(path, sheet_name=sheet, header=[0, 1, 2])

        # Find the indices of sensor data within the DataFrame.
        # df.columns = pd.MultiIndex.from_arrays([df.iloc[0], df.iloc[1]])
        # Extract sensor data.
        data_sensors = []
        sheet_headers = df.keys()
        sheet_name = sheet_headers[0][0]
        timestamps = df[df.columns[0]]

        indices_vwc = []
        indices_st = []
        indices_sc = []
        labels_sensors = []
        port_to_sensor = {}
        for index, header in enumerate(sheet_headers):
            measurement = header[-1]
            port = header[0]
            if "Water Content" in measurement:
                port_to_sensor[port] = len(indices_vwc)
                indices_vwc.append(index)
                indices_st.append(None)
                indices_sc.append(None)
                labels_sensors.append(
                    "_".join(
                        [
                            sheet_name,
                            header[0].replace(" ", ""),
                            header[1].replace(" ", ""),
                        ]
                    )
                )
                data_sensors.append([])
            elif "Soil Temperature" in measurement and port in port_to_sensor:
                indices_st[port_to_sensor[port]] = index
            elif "Bulk EC" in measurement and port in port_to_sensor:
                indices_sc[port_to_sensor[port]] = index

        for index_row, row in df.iterrows():
            for index_sensor, index_swc in enumerate(indices_vwc):
                # Filter out entries with "nan".
                if isinstance(row.iloc[index_swc], float):
                    idx_st = indices_st[index_sensor]
                    idx_sc = indices_sc[index_sensor]
                    st_val = row.iloc[idx_st] if idx_st is not None else None
                    sc_val = row.iloc[idx_sc] if idx_sc is not None else None
                    data_sensors[index_sensor].append(
                        Datum(
                            timestamp=timestamps.iloc[index_row],
                            VWC=row.iloc[index_swc],
                            ST=st_val if isinstance(st_val, float) else None,
                            SC=sc_val if isinstance(sc_val, float) else None,
                        )
                    )

        # Form the Sensor data type.
        for index, data in enumerate(data_sensors):
            try:
                if index < 3 or len(location_labels) == 1:
                    loc_label = location_labels[0]
                else:
                    loc_label = location_labels[1]
                new_sensor = Sensor(
                    coordinates=coord_lut[loc_label],
                    crs="UTM",
                    data=data,
                    depth=depth_lut[index],
                    layer=0,
                    name="-".join(
                        [
                            loc_label,
                            str(depth_lut[index]).replace(".", "_"),
                            labels_sensors[index],
                        ]
                    ),
                )
                # Only generate sensor files when valid data are present.
                if not np.isnan([datum.VWC for datum in data]).all():
                    sensors.append(new_sensor)
            except Exception as e:
                print(f"[WARNING]   Skipping {path}: {e}")

    return sensors


"""_sensors_align_data(sensors)

Truncate sensor data to only include samples taken at matching timestamps.

@param  sensors
@return
"""


def _sensors_align_data(sensors: list[Sensor]) -> list[Sensor]:
    timestamps = []
    # Extract all unique timestamps used.
    for sensor in sensors:
        for datum in sensor.data:
            if datum.timestamp not in timestamps:
                timestamps.append(datum.timestamp)
    timestamps.sort()

    # Remove timestamps from list if not included in at least one sensor's data.
    for index, timestamp in enumerate(timestamps):
        for sensor in sensors:
            if timestamp not in [datum.timestamp for datum in sensor.data]:
                # Set the timestamp to 'None' as a flag.
                timestamps[index] = None
                break

    timestamps = [timestamp for timestamp in timestamps if timestamp != None]

    print(f"PARSED TS: {str(timestamps)}, {str(len(timestamps))}")

    # Now update the sensors list to only include shared timestamps.
    for sensor in sensors:
        data = []
        for datum in sensor.data:
            if datum.timestamp in timestamps:
                data.append(datum)
        sensor.data = data

    return sensors


"""transform(paths_files, path_dir, output_dir)

"""


def transform(
    paths_files: list[str] = [],
    path_dir: str = "",
    output_dir: str = "",
    verbose: bool = False,
):
    # Add any files located in directory specified.
    paths_files += _get_xlsx_files_from_dir(path_dir)

    # Convert logs to Sensors, grouped by logger file.
    print("Ingesting data from:")
    [print(f"\t+ {path}") for path in paths_files]
    sensors_by_logger = {}
    for path in paths_files:
        logger_name = os.path.splitext(os.path.basename(path))[0]
        sensorsFromXlsx = _convert_xlsx_to_sensors(path)
        sensors_by_logger[logger_name] = sensorsFromXlsx
        for sensor in sensorsFromXlsx:
            print(sensor.name)

    # Compute the arithmetic means of each sensor.
    print("Means of each sensor:")
    for sensors in sensors_by_logger.values():
        for sensor in sensors:
            mean = np.mean([datum.VWC for datum in sensor.data])
            print(f"\t+{sensor.name}: {str(mean)}")

    # Display charts interactively — one figure per logger.
    for logger_name, sensors in sensors_by_logger.items():
        fig, axes = plt.subplots(3, 1, sharex=True, layout="constrained")
        fig.suptitle(logger_name)

        # VWC
        for sensor in sensors:
            timestamps = [datum.timestamp for datum in sensor.data]
            values = [datum.VWC for datum in sensor.data]
            if any(v is not None and not np.isnan(v) for v in values):
                axes[0].plot(timestamps, values, label=sensor.name)
        axes[0].set_title("Volumetric Water Content (VWC)")
        axes[0].set_ylabel("VWC")
        axes[0].legend(fontsize="small")

        # SC
        for sensor in sensors:
            timestamps = [datum.timestamp for datum in sensor.data]
            values = [datum.SC for datum in sensor.data]
            if any(v is not None for v in values):
                axes[1].plot(timestamps, values, label=sensor.name)
        axes[1].set_title("Soil Conductivity (SC)")
        axes[1].set_ylabel("SC")
        axes[1].legend(fontsize="small")

        # ST
        for sensor in sensors:
            timestamps = [datum.timestamp for datum in sensor.data]
            values = [datum.ST for datum in sensor.data]
            if any(v is not None for v in values):
                axes[2].plot(timestamps, values, label=sensor.name)
        axes[2].set_title("Soil Temperature (ST)")
        axes[2].set_ylabel("ST")
        axes[2].set_xlabel("Time")
        axes[2].legend(fontsize="small")

    plt.show()

    """
    # Read janky cache.
    sensors = []
    for path in os.listdir("."):
        if "geojson" in path:
            sensor = geojson2sensor(path)
            if sensor:
                sensors.append(sensor)

    # Do stats.
    try:
        data = []
        for sensor in sensors:
            print(sensor.name)
            data.append([datum.VWC for datum in sensor.data])
        print(np.cov(data))
    except Exception as e:
        print(e)
    """


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f",
        "--files",
        default="",
        help="Specify comma-separated paths to sensor data files.",
        type=str,
    )
    parser.add_argument(
        "-d",
        "--dir",
        default="",
        help="Specify path to directory containing sensor data.",
        type=str,
    )
    parser.add_argument(
        "-o",
        "--output",
        default="./out",
        help='Specify path to output directory; defaults to "./out/".',
        type=str,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=False,
        type=bool,
    )
    args = parser.parse_args()
    if args.files:
        paths_files = args.files.split(",")  # Convert file paths to a list.
    else:
        paths_files = []
    transform(
        paths_files=paths_files,
        path_dir=args.dir,
        output_dir=args.output,
        verbose=args.verbose,
    )
