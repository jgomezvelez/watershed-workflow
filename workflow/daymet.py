"""Download DayMet data given a watershed.

DayMet is downloaded in box mode based on watershed bounds, then it can be converted to
hdf5 files that models can read.
"""

import requests
import datetime
import logging
import h5py, netCDF4
import sys, os
import numpy as np
import time
import workflow
import rasterio
# import scipy
from scipy.signal import savgol_filter
import xarray as xr
import dask
import re

VALID_VARIABLES = ['tmin', 'tmax', 'prcp', 'srad', 'vp', 'swe', 'dayl']

class Date:
    """Struct to store day of year and year."""
    def __init__(self, doy, year):
        self.doy = doy
        self.year = year

    def __repr__(self):
        return '{}-{}'.format(self.doy, self.year)
    
def stringToDate(s):
    """convert string to Date format (s.doy, s.year)"""
    if len(s) == 4:
        return Date(1, int(s))

    doy_year = s.split('-')
    if len(doy_year) != 2 or len(doy_year[1]) != 4:
        raise RuntimeError('Invalid date format: {}, should be DOY-YEAR'.format(s))

    return Date(int(doy_year[0]), int(doy_year[1]))

def numDays(start, end):
    """Time difference -- assumes inclusive end date."""
    return 365 * (end.year + 1 - start.year) - (start.doy-1) - (365-end.doy)

def loadFile(fname, var):
    with netCDF4.Dataset(fname, 'r') as nc:
        x = nc.variables['x'][:] * 1000. # km to m; raw netCDF file has km unit 
        y = nc.variables['y'][:] * 1000. # km to m
        time = nc.variables['time'][:]
        assert(len(time) == 365)
        val = nc.variables[var][:]
    return x,y,val

def initData(d, vars, num_days, nx, ny):
    for v in vars:
        # d[v] has shape (nband, nrow, ncol)
        d[v] = np.zeros((num_days, ny, nx),'d')

def collectDaymet(bounds, start, end, crs, vars='all', force=False, combine = False):
    """Calls the DayMet Rest API to get data and save raw data.
    Parameters:
    bounds: fiona or shapely shape, or [xmin, ymin, xmax, ymax]
          Collect a file that covers this shape or bounds.
    start: str
        start date in the format of "doy-year", e.g., "1-2012"
    end: str
        end date in the format of "doy-year", e.g., "365-2012"     
    crs : CRS object
          Coordinate system of the above polygon_or_bounds           
    vars: list or 'all'
        list of strings that are in VALID_VARIABLES. Default is use all available variables.
    force : bool
        Download or re-download the file if true.    
    combine : bool
        whether to combine downloaded raw files into a single NetCDF4 file. Default is False.
    """
    T0 = time.time()

    if isinstance(start, str) or isinstance(end, str):
        start = stringToDate(start)
        end = stringToDate(end)

    if vars == 'all':
        vars = VALID_VARIABLES
        logging.info(f"downloading variables: {VALID_VARIABLES}")

    dat = dict()
    d_inited = False

    daymet_obj = workflow.sources.manager_daymet.FileManagerDaymet()

    for year in range(start.year, end.year+1):
        for var in vars:

            fname, feather_bounds = daymet_obj.get_meteorology(var, year, bounds, crs, force_download = force)
            x,y,v = loadFile(fname, var) # returned v.shape(nband, nrow, ncol)
            if not d_inited:
                initData(dat, vars, numDays(start,end), len(x), len(y))
                d_inited = True

            # stuff v in the right spot
            if year == start.year and year == end.year:
                dat[var][:,:,:] = v[start.doy-1:end.doy,:,:]
            elif year == start.year:
                dat[var][0:365-start.doy+1,:,:] = v[start.doy-1:,:,:]
            elif year == end.year:
                dat[var][-end.doy:,:,:] = v[-end.doy:,:,:]
            else:
                my_start = 365 * (year - start.year) - start.doy + 1
                dat[var][my_start:my_start+365,:,:] = v

    if combine:
        logging.info(f"Combining all NetCDF into one...")
        path = '/'.join(re.split(r'\/', fname)[:-1]) + "/"
        all_daymet = f"daymet_*_*_{feather_bounds[3]}x{feather_bounds[0]}_{feather_bounds[1]}x{feather_bounds[2]}.nc"
        dset = xr.open_mfdataset(path +  all_daymet)
        outfile = path + f"daymet_all_{start.doy}-{start.year}_{end.doy}-{end.year}.nc"
        dset.to_netcdf(outfile, format = "NETCDF4")
        logging.info(f"writing a single NetCDF to : {outfile}")
    logging.info(f'seconds to write: {time.time()-T0} s')
    return dat, x, y

def reproj_Daymet(x, y, raw, dst_crs, resolution = None):
    """
    reproject daymet raw data to watershed CRS.
    Parameters:
        x: list
            x-coordinates
        y: list
            y-coordinates
        raw: dict
            raw data input
        dst_crs: workflow crs
            destination CRS
        resolution: float, default is letting rasterio to guess the best closest resolution to raw data
            resolution for the new data
    """
    var_list = list(raw.keys())
    logging.debug(f"variables: {var_list}")
    logging.debug(f"raw shape in (nband, nrow, ncol): {raw[var_list[0]].shape}")
    
    if raw[var_list[0]].ndim == 3:
        nband = raw[var_list[0]].shape[0]   
    else:
        nband = 1 
    
    daymet_crs = workflow.crs.daymet_crs()
    logging.debug(f'daymet crs: {daymet_crs}')
    
    # make sure tranform function is consistent with the unit used in CRS
    unit = daymet_crs.to_dict()['units']
    if unit == 'km':
        dx = dy = 1.0 # km
        transform = (x.min()/1000 - dx/2, dx, 0.0, y.max()/1000 + dy/2, 0.0, -dy) # accepted format(xmin, dx, 0, ymax, 0, -dy)
        affine = rasterio.transform.from_origin(x.min() - dx/2, y.max() + dy/2, dx, dy)
    elif unit == 'm':
        dx = dy = 1000.0 # m
        transform = (x.min() - dx/2, dx, 0.0, y.max() + dy/2, 0.0, -dy)
        affine = rasterio.transform.from_origin(x.min() - dx/2, y.max() + dy/2, dx, dy)
    else: 
        raise RuntimeError(f'Daymet CRS unit: {unit} is not recognized! Supported units are m or km.')
    logging.debug(f'transform: {transform}')
    logging.debug(f'Affine: {affine}') 
    
    daymet_profile = {
        'driver': 'GTiff', 
        'dtype': 'float32', 
        'nodata': -9999.0, 
        'width': len(x), 
        'height': len(y), 
        'count': nband, 
        'crs':daymet_crs,
        'transform':affine,
        'tiled': False, 
        'interleave': 'pixel'
    }

    logging.info(f'daymet profile: {daymet_profile}') 
    
    logging.info(f'reprojecting to new crs: {dst_crs}') 
    new_dat = {}
    for var in var_list:

        idat = raw[var]
        dst_profile, dst_raster = workflow.warp.raster(src_profile=daymet_profile, src_array=idat, 
                                    dst_crs=dst_crs, resolution = resolution)
        new_dat[var] = dst_raster    
    
    logging.info(f"new profile: {dst_profile}")
    new_extent = rasterio.transform.array_bounds(dst_profile['height'], dst_profile['width'], dst_profile['transform']) # (x0, y0, x1, y1)

    logging.info(f"new extent[xmin, ymin, xmax, ymax]: {new_extent}")
    
    new_x, new_y = xy_from_profile(dst_profile)
    
    return new_x, new_y, new_extent, new_dat, daymet_profile

def smoothRaw(raw, smooth_filter = True, nyears = None):
    """Smooth daymet to get a typical year by averaging each day across all years. Optionally can apply 
    a moving average filter to get smoother time series data.
    Parameters:
        raw, dict
            raw daymet
        smooth_filter, bool
            whether to use savgol_filter for smoothing
        nyears, int
            repreat the typical year for how many years
    """
    logging.info("averaging daymet by taking the average for each day across the actual years.")
    var_list = list(raw.keys())
    if nyears == None:
        nyears = raw[var_list[0]].shape[0]//365 
    # reshape dat
    smooth_dat = dict()
    for ivar in var_list:
        idat = raw[ivar]
        if nyears*365 != idat.shape[0]:
            idat = idat[0:nyears*365, :, :]
        idat = idat.reshape(nyears, 365, idat.shape[1], idat.shape[2])
        # # average over years so that the dat has shape of (365, nx, ny)
        inew = idat.mean(axis = 0)
        
        # apply smooth filter
        if smooth_filter:
            window = 61
            poly_order = 2
            logging.info(f"smoothing {ivar} using savgol filter, window = {window} d, poly order = {poly_order}")
            inew = savgol_filter(inew, window, poly_order, axis = 0, mode = 'wrap')  
        # repeat this for nyears
        smooth_dat[ivar] = np.tile(inew, (nyears, 1, 1))   

    return smooth_dat

def daymetToATS(dat, smooth = False, smooth_filter = True, nyears = None):
    """Accepts a numpy named array of DayMet data and returns a dictionary ATS data. 
    It has the option to smooth the data.
    Parameters:
        dat, dict
            daymet dictinary
        smooth, bool
            whether to smooth the data or not
        smooth_filter, bool
            whether to apply a savgol filter for smoothing. Default is True.
        nyears, int
            how many years to repeat the typical year. Default is to get the same length as the raw data.
    """
    logging.info(f"input dat shape: {dat[list(dat.keys())[0]].shape}")
    dout = dict()
    logging.info('Converting to ATS met input')
    
    if smooth:
        dat = smoothRaw(dat, smooth_filter = smooth_filter, nyears = nyears)
        logging.info(f"shape of smoothed dat is {dat[list(dat.keys())[0]].shape}")
    mean_air_temp_c = (dat['tmin'] + dat['tmax'])/2.0
    precip_ms = dat['prcp'] / 1.e3 / 86400. # mm/day --> m/s
    
    # Sat vap. press o/water Dingman D-7 (Bolton, 1980)
    sat_vp_Pa = 611.2 * np.exp(17.67 * mean_air_temp_c / (mean_air_temp_c + 243.5))

    time = np.arange(0, dat[list(dat.keys())[0]].shape[0], 1)*86400.

    dout['air temperature [K]'] = 273.15 + mean_air_temp_c # K
    dout['incoming shortwave radiation [W m^-2]'] = dat['srad'] # Wm2
    dout['relative humidity [-]'] = np.minimum(1.0, dat['vp']/sat_vp_Pa) # -
    dout['precipitation rain [m s^-1]'] = np.where(mean_air_temp_c >= 0, precip_ms, 0)
    dout['precipitation snow [m SWE s^-1]'] = np.where(mean_air_temp_c < 0, precip_ms, 0)
    dout['wind speed [m s^-1]'] = 4. * np.ones_like(dout['relative humidity [-]'])

    logging.debug(f"output dout shape: {dout['incoming shortwave radiation [W m^-2]'].shape}")
    return time, dout

def writeATS(dat, x, y, attrs, filename, **kwargs):
    """Accepts a dictionary of ATS data and writes it to HDF5 file."""

    logging.info('Writing ATS file: {}'.format(filename))

    try:
        os.remove(filename)
    except FileNotFoundError:
        pass
    
    time, dat = daymetToATS(dat, **kwargs)

    with h5py.File(filename, 'w') as fid:
        fid.create_dataset('time [s]', data=time)
        assert(len(x.shape) == 1)
        assert(len(y.shape) == 1)
       
        # ATS requires increasing order for y
        rev_y = y[::-1]
        fid.create_dataset('row coordinate [m]', data=rev_y) 
        fid.create_dataset('col coordinate [m]', data=x)

        for key in dat.keys():
            # dat has shape (nband, nrow, ncol) 
            assert(dat[key].shape[0] == time.shape[0])
            assert(dat[key].shape[1] == y.shape[0])
            assert(dat[key].shape[2] == x.shape[0])
            # dat[key] = dat[key].swapaxes(1,2) # reshape to (nband, nrow, ncol)
            grp = fid.create_group(key)
            for i in range(len(time)):
                idat = dat[key][i,:,:]
                # flip rows to match the order of y, so it starts with (x0,y0) in the upper left
                rev_idat = np.flip(idat, axis=0)
                
                grp.create_dataset(str(i), data=rev_idat)

        for key, val in attrs.items():
            fid.attrs[key] = val

    return time, dat

def getAttrs(bounds, start, end):
    # set the wind speed height, which is made up
    attrs = dict()
    attrs['wind speed reference height [m]'] = 2.0
    attrs['DayMet x min'] = bounds[1]
    attrs['DayMet y min'] = bounds[0]
    attrs['DayMet x max'] = bounds[3]
    attrs['DayMet y max'] = bounds[2]
    attrs['DayMet start date'] = str(start)
    attrs['DayMet end date'] = str(end)
    return attrs    

def writeHDF5(dat, x, y, attrs, filename):
    """Write daymet to a single HDF5 file."""
    logging.info('Writing HDF5 file: {}'.format(filename))

    try:
        os.remove(filename)
    except FileNotFoundError:
        pass

    time = np.arange(0, dat[list(dat.keys())[0]].shape[0], 1)
    with h5py.File(filename, 'w') as fid:
        fid.create_dataset('time [d]', data=time)
        assert(len(x.shape) == 1)
        assert(len(y.shape) == 1)
       
        # make y increasing order
        rev_y = y[::-1]
        fid.create_dataset('y [m]', data=rev_y) 
        fid.create_dataset('x [m]', data=x)

        for key in dat.keys():
            # dat has shape (nband, nrow, ncol) 
            assert(dat[key].shape[0] == time.shape[0])
            assert(dat[key].shape[1] == y.shape[0])
            assert(dat[key].shape[2] == x.shape[0])

            grp = fid.create_group(key)
            for i in range(len(time)):
                idat = dat[key][i,:,:]
                # flip rows to match the order of y, so it starts with (x0,y0) in the upper left
                rev_idat = np.flip(idat, axis=0)               
                grp.create_dataset(str(i), data=rev_idat)

        for key, val in attrs.items():
            fid.attrs[key] = val

    return    



def validBounds(bounds):
    return True

def xy_from_profile(profile):
    """
    get x, y coord from raster profile.
    """
    xmin = profile['transform'][2]
    ymax = profile['transform'][5]
    dx = profile['transform'][0]
    dy = -profile['transform'][4]
    nx = profile['width']
    ny = profile['height']

    x = xmin + dx/2 + np.arange(nx) * dx
    y = ymax - dy/2 - np.arange(ny) * dy
    
    return x, y