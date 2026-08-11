import json
d = json.load(open('D:/work/ac-advisor/forecast.json', encoding='utf-8'))['daily']
dates = d['time']; tmax = d['temperature_2m_max']; tmin = d['temperature_2m_min']
rain = d['precipitation_probability_max']; hum = d['relative_humidity_2m_mean']
print('日期        最高/最低°C  湿度%  降雨%')
print('-'*40)
for i in range(len(dates)):
    print(f'{dates[i]}  {tmax[i]:>4.1f}/{tmin[i]:>4.1f}   {hum[i]:>4.0f}%   {rain[i]:>3.0f}%')