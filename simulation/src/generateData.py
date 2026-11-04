import numpy as np
import pandas as pd

# 1. Create a datetime index (hourly for 100 periods)
time_index = pd.date_range(
    start="2026-08-10", end="2026-08-11", freq="3s", inclusive="left"
)
unix_seconds = time_index.astype("datetime64[s]").astype("int64")
print(len(unix_seconds), "time index length")
# 2. Generate a continuous sine wave (e.g., completing 4 full cycles)
sine_values = (np.sin(np.linspace(0, 2 * np.pi, 28800)) + 1) * 5
print(len(sine_values), "sine values length")

# 3. Combine into a DataFrame
df = pd.DataFrame({"timestamp": unix_seconds, "temperature": sine_values})

# 4. Save the DataFrame to a CSV file
df.to_csv("sine_time_series.csv", index=False)

print("CSV file 'sine_time_series.csv' successfully created!")
