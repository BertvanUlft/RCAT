#!/usr/bin/env python
# coding: utf-8

# Import modules

import sys
import os
import glob
import ini_reader
import xarray as xa
import xesmf as xe
from itertools import product
import grid_interpolation as remap
import geosfuncs as gfunc
import dask.array as da
import numpy as np
from dask.distributed import Client
from dask_jobqueue import SLURMCluster
import statistics as st

import warnings
warnings.filterwarnings("ignore")


# Functions
def get_settings(config_file):
    """
    Retrieve information from main configuration file
    """
    conf_dict = ini_reader.get_config_dict(config_file)
    d = {
        'models': conf_dict['MODELS'],
        'obs start year': conf_dict['OBS']['start year'],
        'obs end year': conf_dict['OBS']['end year'],
        'obs months': conf_dict['OBS']['months'],
        'variables': conf_dict['SETTINGS']['variables'],
        'regions': conf_dict['SETTINGS']['regions'],
        'requested_stats': conf_dict['STATISTICS']['stats'],
        'stats_conf': st.mod_stats_config(conf_dict['STATISTICS']['stats']),
        'validation_plot': conf_dict['PLOTTING']['validation plot'],
        'map configure': conf_dict['PLOTTING']['map configure'],
        'map grid setup': conf_dict['PLOTTING']['map grid setup'],
        'map kwargs': conf_dict['PLOTTING']['map kwargs'],
        'line grid setup': conf_dict['PLOTTING']['line grid setup'],
        'line kwargs': conf_dict['PLOTTING']['line kwargs'],
        'nodes': conf_dict['SLURM']['nodes'],
        'slurm kwargs': conf_dict['SLURM']['slurm kwargs'],
        'outdir': conf_dict['SETTINGS']['output dir'],
    }

    return d


def slurm_cluster_setup(nodes=1, **kwargs):
    """
    Set up SLURM cluster

    Parameters
    ----------
    nodes: int
        Number of nodes to use
    **kwargs:
        Keyword arguments for cluster specifications
    """
    cluster = SLURMCluster(**kwargs)
    cluster.scale(nodes)
    return cluster


def get_args():
    """
    Read configuration file
    Parameters
    ----------
    -
    Returns
    -------
    Input arguments
    """
    import argparse

    # Configuring argument setup and handling
    parser = argparse.ArgumentParser(
        description='Main script for model/obs validation')
    parser.add_argument('--config', '-c',  metavar='name config file',
                        type=str, help='<Required> Full path to config file',
                        required=True)
    return parser.parse_args()


def get_grid_coords(nc, grid_coords):
    """
    Read model grid coordinates

    Parameters
    ----------
    nc: xarray dataset
    Returns
    -------
    grid_coords: dict
    """

    def _domain(lats, lons, x):
        lons_p = np.r_[lons[x, x:-x], lons[x:-x, -1-x][1:],
                       lons[-1-x, x:-x][::-1][1:], lons[x:-x, x][::-1][1:]]
        lats_p = np.r_[lats[x, x:-x], lats[x:-x, -1-x][1:],
                       lats[-1-x, x:-x][::-1][1:], lats[x:-x, x][::-1][1:]]
        return list(zip(lons_p, lats_p))

    grid_coords['lat_0'] = nc['Lambert_Conformal'].attrs[
        'latitude_of_projection_origin']
    grid_coords['lon_0'] = nc['Lambert_Conformal'].attrs[
        'longitude_of_central_meridian']
    gp_bfr = 15
    grid_coords['crnrs'] = [nc.lat.values[gp_bfr, gp_bfr],
                            nc.lon.values[gp_bfr, gp_bfr],
                            nc.lat.values[-gp_bfr, -gp_bfr],
                            nc.lon.values[-gp_bfr, -gp_bfr]]
    grid_coords['domain'] = _domain(nc.lat.values, nc.lon.values, gp_bfr)

    return grid_coords


def get_grids(nc, target_grid, method='bilinear'):
    """
    Get and/or modify the source and target grids for interpolation
    """
    if method == 'conservative' and 'lon_bnds' not in nc.data_vars:
        print("--- Lon/lat bounds not in file! ---")
        print("Calculating them internally ...")
        print()
        slon_b, slat_b = remap.fnCellCorners(nc.lon.values, nc.lat.values)
        tlon_b, tlat_b = remap.fnCellCorners(target_grid['lon'],
                                             target_grid['lat'])
        s_grid = {'lon': nc.lon.values, 'lat': nc.lat.values,
                  'lon_b': slon_b, 'lat_b': slat_b}
        t_grid = {'lon': target_grid['lon'], 'lat': target_grid['lat'],
                  'lon_b': tlon_b, 'lat_b': tlat_b}
    else:
        s_grid = {'lon': nc.lon.values, 'lat': nc.lat.values}
        t_grid = target_grid
    if t_grid['lon'].ndim == 1:
        outdims = (t_grid['lat'].size, t_grid['lon'].size)
        outdims_shape = {'lon': ('x',), 'lat': ('y',)}
    else:
        outdims = (t_grid['lon'].shape[0], t_grid['lon'].shape[1])
        outdims_shape = {'lon': ('y', 'x'), 'lat': ('y', 'x')}

    return s_grid, t_grid, outdims, outdims_shape


def remap_func(dd, v, vconf, mnames, onames, gdict):
    if vconf['regrid'] is None:
        gdict.update(
            {'lon': {m: dd[m]['grid']['lon'] for m in mnames},
             'lat': {m: dd[m]['grid']['lat'] for m in mnames}})
        if None not in onames:
            for obs in onames:
                gdict['lon'].update({obs: dd[obs]['grid']['lon']})
                gdict['lat'].update({obs: dd[obs]['grid']['lat']})
        gridname = 'native_grid'
    else:
        if vconf['regrid'] in onames:
            oname = vconf['regrid']
            target_grid = dd[oname]['grid']
            gridname = dd[oname]['gridname']
            gdict.update({'lon': {oname: target_grid['lon']},
                          'lat': {oname: target_grid['lat']}})
            # Perform the regridding
            for m in mnames:
                rgr_data = regridding(dd, m, v, target_grid,
                                      vconf['rgr_method'])
                dd[m]['data'] = rgr_data
            if len(onames) > 1:
                obslist = onames.copy()
                obslist.remove(oname)
                for obs in obslist:
                    rgr_data = regridding(dd, obs, v, target_grid,
                                          vconf['rgr_method'])
                    dd[obs]['data'] = rgr_data
        elif vconf['regrid'] in mnames:
            mname = vconf['regrid']
            modlist = mnames.copy()
            modlist.remove(mname)
            target_grid = dd[mname]['grid']
            gridname = dd[mname]['gridname']
            gdict.update({'lon': {mname: target_grid['lon']},
                          'lat': {mname: target_grid['lat']}})
            for mod in modlist:
                rgr_data = regridding(dd, mod, v, target_grid,
                                      vconf['rgr_method'])
                dd[mod]['data'] = rgr_data
            if None not in onames:
                for obs in onames:
                    rgr_data = regridding(dd, obs, v, target_grid,
                                          vconf['rgr_method'])
                    dd[obs]['data'] = rgr_data

    gdict.update({'gridname': gridname})

    return dd, gdict


def regridding(data, data_name, var, target_grid, method):
    print("\n** Regridding {} data **\n".format(data_name.upper()))

    indata = data[data_name]['data']

    # This works. Replace with xarray apply_ufunc?
    data_dist = client.persist(indata[var].data)
    sgrid, tgrid, dim_val, dim_shape = get_grids(indata, target_grid, method)
    regridder = remap.add_matrix_NaNs(xe.Regridder(sgrid, tgrid, method))
    A = regridder.A
    rgr_data = da.map_blocks(_rgr_calc, data_dist, dtype=float,
                             chunks=(indata.chunks['time'],) + dim_val,
                             A=A, out_dims=dim_val)
    rgr_data = client.persist(rgr_data)  # IS THIS NEEDED?

    # Contain dask in xarray dataset
    darr = xa.DataArray(
        rgr_data, name=var,
        coords={'lon': (dim_shape['lon'], target_grid['lon']),
                'lat': (dim_shape['lat'], target_grid['lat']),
                'time': indata.time.values},
        dims=['time', 'y', 'x'], attrs=indata.attrs.copy())

    dset = darr.to_dataset()
    del data_dist, rgr_data

    return dset


def _rgr_calc(data, A, out_dims):
    """
    Regrid data with sparse matrix multiplication
    For more info, see xESMF python module docs and code
    """
    s = data.shape
    Ny_in, Nx_in = (s[-2], s[-1])
    indata_flat = data.reshape(-1, Ny_in*Nx_in)
    outdata_flat = A.dot(indata_flat.T).T
    N_extra_list = s[0:-2]
    Ny_out, Nx_out = out_dims
    outdata = outdata_flat.reshape([*N_extra_list, Ny_out, Nx_out])

    return outdata


def calc_stats(ddict, vv, stat, pool, chunk_dim, stats_config, regions):
    """
    Calculate statistics for variables and models/obs
    """
    st_data = {}
    for v, f in vv.items():
        st_data[v] = {}
        for m in ddict[f][v]:
            print("Calculate {} {} for {}\n".format(v, stat, m))

            if ddict[f][v][m] is None:
                print("* Data not available for model {}".format(m))
                print("* Moving on ...\n")
            else:
                indata = ddict[f][v][m]['data']
                st_data[v][m] = {}
                if len(indata.data_vars) > 2:
                    indata_tmp = indata[v]
                    indata = indata_tmp.to_dataset()

                if stats_config[stat]['resample resolution'] is not None:
                    from pandas import to_timedelta
                    diff = indata.time.values[1] - indata.time.values[0]
                    nsec = diff.astype('timedelta64[s]')/np.timedelta64(1, 's')
                    tresample = stats_config[stat]['resample resolution']
                    sec_resample = to_timedelta(tresample[0]).total_seconds()
                    if nsec != sec_resample:
                        indata = eval("indata.resample(time='{}').{}('time').\
                                      dropna('time', 'all')".format(
                                          tresample[0], tresample[1]))

                # Check chunking of data
                data = manage_chunks(indata, chunk_dim)
                st_data[v][m]['domain'] = st.calc_statistics(data, v, stat,
                                                             stats_config)
                if regions:
                    masks = {
                        r: gfunc.reg_mask(data.lon.values, data.lat.values,
                                          r, cut_data=False) for r in regions}
                    if pool:
                        mdata = {r: get_masked_data(data, v, masks[r])
                                 for r in regions}

                        st_mdata = {r: st.calc_statistics(mdata[r], v, stat,
                                                          stats_config)
                                    for r in regions}
                    else:
                        st_mdata = {r: get_masked_data(st_data[v][m]['domain'],
                                                       v, masks[r])
                                    for r in regions}

                    st_data[v][m]['regions'] = st_mdata

    return st_data


def get_month_string(mlist):
    d = {1: 'J', 2: 'F', 3: 'M', 4: 'A', 5: 'M', 6: 'J', 7: 'J', 8: 'A',
         9: 'S', 10: 'O', 11: 'N', 12: 'D'}
    if len(mlist) == 12:
        mstring = 'ANN'
    else:
        if mlist == (1, 2, 12):
            mlist = (12, 1, 2)
        mstring = ''.join(d[m] for m in mlist)
    return mstring


def save_to_disk(data, label, stat, odir, var, grid, sy, ey, tsuffix,
                 stat_dict, tres, thr='', regs=None):
    """
    Saving statistical data to netcdf files
    """
    # encoding = {'lat': {'dtype': 'float32', '_FillValue': False},
    #             'lon': {'dtype': 'float32', '_FillValue': False},
    #             var: {'dtype': 'float32', '_FillValue': 1.e20}
    #             }
    if stat in ('annual cycle', 'seasonal cycle', 'diurnal cycle'):
        tstat = '_' + stat_dict['time stat'].replace(' ', '_')
    else:
        tstat = ''
    stat_name = stat.replace(' ', '_')
    if regs is not None:
        for r in regs:
            rn = r.replace(' ', '_')
            data['regions'][r].attrs['Analysed time'] =\
                "{}-{} | {}".format(sy, ey, tsuffix)
            fname = '{}_{}_{}_{}{}{}_{}_{}_{}-{}_{}.nc'.format(
                label, stat_name, var, thr, tres, tstat, rn, grid, sy, ey,
                tsuffix)
            data['regions'][r].to_netcdf(os.path.join(odir, stat_name, fname))
    fname = '{}_{}_{}_{}{}{}_{}_{}-{}_{}.nc'.format(
        label, stat_name, var, thr, tres, tstat, grid, sy, ey, tsuffix)
    data['domain'].attrs['Analysed time'] = "{}-{} | {}".format(sy, ey,
                                                                tsuffix)
    data['domain'].to_netcdf(os.path.join(odir, stat_name, fname))


def get_masked_data(data, var, mask):
    """
    Mask region
    """
    mask_in = xa.DataArray(np.broadcast_to(mask, data[var].shape),
                           dims=data[var].dims)
    return data.where(mask_in, drop=True)

    # P. Lind 2019-10-29
    # N.B. For masking of large data sets, the code below might be a more
    # viable choice.
    #
    # def _mask_func(arr, axis=0, lons=None, lats=None, region=''):
    #     iter_3d = arr.shape[axis]
    #     mdata = gfunc.reg_mask(lons, lats, region, arr, iter_3d=iter_3d,
    #                            cut_data=True)[0]
    #     return mdata

    # mask = gfunc.reg_mask(data.lon.values, data.lat.values, reg,
    #                       cut_data=True)

    # imask = mask[0]
    # mask_data = da.map_blocks(_mask_func, data[var].data, dtype=float,
    #                           chunks=(data.chunks['time'],
    #                                   imask.shape[0], imask.shape[1]),
    #                           lons=data.lon.values, lats=data.lat.values,
    #                           region=reg)
    # lon_d = 'x' if mask[1].ndim == 1 else ['y', 'x']
    # lat_d = 'y' if mask[2].ndim == 1 else ['y', 'x']
    # out = xa.Dataset(
    #     {var: (['time', 'y', 'x'],  mask_data)},
    #     coords={'lon': (lon_d, mask[1]), 'lat': (lat_d, mask[2]),
    #             'time': data.time.values})
    # return out


def manage_chunks(data, chunk_dim):
    """
    Re-chunk the data in other dimension or if number of chunks is too large.
    """
    if chunk_dim == 'space':
        # Space dimensions in different observations have different names. This
        # dictionary is created to account for that (to some extent), but in
        # the future this might be change/removed. For now, it's hard coded.
        spcdim = {'x': ['x', 'X', 'lon', 'rlon'],
                  'y': ['y', 'Y', 'lat', 'rlat']}
        xd = [x for x in data.dims if x in spcdim['x']][0]
        yd = [y for y in data.dims if y in spcdim['y']][0]

        xsize = data[xd].size
        ysize = data[yd].size

        # Max number of chunks
        cmax = 500
        n = np.sqrt((xsize*ysize)/cmax)
        c_size = np.round(np.maximum(xsize, ysize)/n).astype(int)
        data = data.chunk({'time': data.time.size, xd: c_size, yd: c_size})
    else:
        nchunks = len(data.chunks['time'])
        if nchunks > 500:
            rchunk_size = np.round(data.time.size/500).astype(int)
            data = data.chunk({'time': rchunk_size})

    return data


def get_prep_func(deacc):
    """
    Return function that either de-accumulate data
    or slice it.
    """
    if deacc:
        def f(data):
            return data.diff(dim='time')
    else:
        def f(data):
            return data.isel(time=slice(0, -1))

    return f


def get_variable_config(var_conf, var):
    """
    Retrieve configuration info for variable var as defined in main
    configuration file,
    """
    vdict = {
        'input resolution': var_conf['freq'],
        'units': var_conf['units'],
        'scale_factor': var_conf['scale factor'],
        'obs_scale_factor': var_conf['obs scale factor'],
        'prep_func': get_prep_func(var_conf['accumulated']),
        'regrid': var_conf['regrid to'],
        'rgr_method': var_conf['regrid method'],
    }
    return vdict


def get_mod_data(mconf, tres, var, cfactor, prep_func):
    """
    Open model data files where file path is dependent on time resolution tres.
    """
    date_list = ["{}{:02d}".format(yy, mm) for yy, mm in product(
        range(mconf['start year'], mconf['end year']+1), mconf['months'])]
    _flist = [glob.glob(os.path.join(
        mconf['fpath'], '{}/{}_*{}_{}.nc'.format(tres, var, tres, dd)))
        for dd in date_list]
    flist = [y for x in _flist for y in x]

    print("\n** Opening {} files **".format(mod_name.upper()))
    if tres == 'mon':
        mod_data = xa.open_mfdataset(flist, parallel=True)
    else:
        mod_data = xa.open_mfdataset(flist, parallel=True,
                                     preprocess=prep_func)
    if cfactor is not None:
        mod_data[var] *= cfactor

    grid = {'lon': mod_data.lon.values, 'lat': mod_data.lat.values}
    gridname = 'grid_{}_{}'.format(mod_name.upper(), mod_data.attrs['domain'])

    outdata = {'data': mod_data, 'grid': grid, 'gridname': gridname}
    return outdata


def get_obs_data(obs, var, cfactor, sy, ey, mns):
    """
    Open obs data files.
    """
    import observations_metadata as obsmeta
    obs_dict = obsmeta.obs_data()

    sdate = '{}{:02d}'.format(sy, np.min(mns))
    edate = '{}{:02d}'.format(ey, np.max(mns))

    print("\n** Opening {} files **".format(obs.upper()))

    # Open obs files
    obs_flist = obsmeta.get_file_list(var, obs, sdate, edate)
    f_obs = xa.open_mfdataset(obs_flist, parallel=True)

    # Extract years
    obs_data = f_obs.where(((f_obs.time.dt.year >= sy) &
                            (f_obs.time.dt.year <= ey) &
                            (np.isin(f_obs.time.dt.month, mns))),
                           drop=True)
    # Scale data
    if cfactor is not None:
        obs_data[var] *= cfactor

    lons = obs_data.lon.values
    lats = obs_data.lat.values

    # Space dimensions in different observations have different names. This
    # dictionary is created to account for that (to some extent), but in the
    # future this might be change/removed. For now, it's hard coded.
    spcdim = {'x': ['x', 'X', 'lon', 'rlon'],
              'y': ['y', 'Y', 'lat', 'rlat']}
    xd = [x for x in obs_data.dims if x in spcdim['x']][0]
    yd = [y for y in obs_data.dims if y in spcdim['y']][0]

    # Make sure lon/lat elements are in ascending order
    if lons.ndim == 1:
        if np.diff(lons)[0] < 0:
            lons = lons[::-1]
            obs_data = obs_data.reindex({xd: obs_data[xd][::-1]})
        if np.diff(lats)[0] < 0:
            lats = lats[::-1]
            obs_data = obs_data.reindex({yd: obs_data[yd][::-1]})
    elif lons.ndim == 2:
        if np.diff(lons[0, :])[0] < 0:
            lons = np.flipud(lons)
            obs_data = obs_data.reindex({xd: np.flipud(obs_data[xd])})
        if np.diff(lats[:, 0])[0] < 0:
            lats = np.flipud(lats)
            obs_data = obs_data.reindex({yd: np.flipud(obs_data[yd])})

    grid = {'lon': lons, 'lat': lats}
    if obs_dict[var][obs]['grid'] is not None:
        gridname = obs_dict[var][obs]['grid'].split('/')[-1]
    else:
        gridname = 'grid_{}'.format(obs.upper())

    outdata = {'data': obs_data, 'grid': grid, 'gridname': gridname}
    return outdata


def get_plot_dict(cdict, var, grid_coords, models, obs, yrs_d, mon_d,
                  img_outdir, stat_outdir, stat):
    """
    Create a dictionary with settings for a validation plot procedure.
    """
    # Get some settings and meta data
    var_conf = get_variable_config(cdict['variables'][var], var)
    tres = var_conf['input resolution']
    grdnme = grid_coords['target grid'][var]['gridname']
    st = stat.replace(' ', '_')
    if stat in ('annual cycle', 'seasonal cycle', 'diurnal cycle'):
        tstat = '_' + cdict['stats_conf'][stat]['time stat'].replace(' ', '_')
    else:
        tstat = ''
    thrlg = (('thr' in cdict['stats_conf'][stat]) and
             (cdict['stats_conf'][stat]['thr'] is not None) and
             (var in cdict['stats_conf'][stat]['thr']))
    if thrlg:
        thrval = str(cdict['stats_conf'][stat]['thr'][v])
        thrstr = "thr{}_".format(thrval.replace('.', ''))
    else:
        thrstr = ''

    # Create dictionaries with list of files for models and obs
    _fm_list = {stat: [glob.glob(os.path.join(
        stat_outdir, '{}'.format(st), '{}_{}_{}_{}{}{}_{}_{}-{}_{}.nc'.format(
            m, st, var, thrstr, tres, tstat, grdnme, yrs_d[m][0], yrs_d[m][1],
            get_month_string(mon_d[m]))))
                       for m in models]}
    fm_list = {s: [y for x in ll for y in x] for s, ll in _fm_list.items()}

    obs_list = [obs] if not isinstance(obs, list) else obs
    if obs is not None:
        _fo_list = {stat: [glob.glob(os.path.join(
            stat_outdir, '{}'.format(st),
            '{}_{}_{}_{}{}{}_{}_{}-{}_{}.nc'.format(
                o, st, var, thrstr, tres, tstat, grdnme,
                yrs_d[o][0], yrs_d[o][1], get_month_string(mon_d[o]))))
            for o in obs_list]}
        fo_list = {s: [y for x in ll for y in x] for s, ll in _fo_list.items()}
    else:
        fo_list = {stat: None}

    sy = np.unique([n['start year'] for m, n in cdict['models'].items()])
    ey = np.unique([n['end year'] for m, n in cdict['models'].items()])
    if sy.size > 1:
        years = {'T1': (sy[0], ey[0]), 'T2': (sy[1], ey[1])}
    else:
        years = (sy[0], ey[0])

    # Create a dictionary with settings for plotting procedure
    plot_dict = {
        'mod files': fm_list,
        'obs files': fo_list,
        'models': models,
        'observation': obs,
        'variable': var,
        'time res': tres,
        'time stat': tstat,
        'units': var_conf['units'],
        'grid coords': grid_coords,
        'map configure': cdict['map configure'],
        'map grid setup': cdict['map grid setup'],
        'map kwargs': cdict['map kwargs'],
        'line grid setup': cdict['line grid setup'],
        'line kwargs': cdict['line kwargs'],
        'regions': cdict['regions'],
        'years': years,
        'img dir': os.path.join(img_outdir, st)
    }

    # If there are regions, create list of files for these  as well
    # Then also update plot dictionary
    if cdict['regions'] is not None:
        _fm_listr = {stat: {r:  [glob.glob(os.path.join(
            stat_outdir, '{}'.format(st),
            '{}_{}_{}_{}{}{}_{}_{}_{}-{}_{}.nc'.format(
                m, st, var, thrstr, tres, tstat, r, grdnme,
                yrs_d[m][0], yrs_d[m][1], get_month_string(mon_d[m]))))
            for m in models] for r in cdict['regions']}}
        fm_listr = {s: {r: [y for x in _fm_listr[s][r] for y in x]
                        for r in _fm_listr[s]} for s in _fm_listr}
        if obs is not None:
            _fo_listr = {stat: {r: [glob.glob(os.path.join(
                stat_outdir, '{}'.format(st),
                '{}_{}_{}_{}{}{}_{}_{}_{}-{}_{}.nc'.format(
                    o, st, var, thrstr, tres, tstat, r, grdnme,
                    yrs_d[o][0], yrs_d[o][1], get_month_string(mon_d[o]))))
                for o in obs_list] for r in cdict['regions']}}
            fo_listr = {s: {r: [y for x in _fo_listr[s][r] for y in x]
                            for r in _fo_listr[s]} for s in _fo_listr}
        else:
            fo_listr = {stat: None}

        plot_dict.update({
            'mod reg files': fm_listr,
            'obs reg files': fo_listr,
        })

    return plot_dict


# Configuration
args = get_args()

# Read config file
config_file = args.config
cdict = get_settings(config_file)

###############################################
#                                             #
#            0. CREATE DIRECTORIES            #
#                                             #
###############################################

# Create dirs
stat_outdir = os.path.join(cdict['outdir'], 'stats')
stat_names = [s.replace(' ', '_') for s in cdict['requested_stats']]
img_outdir = os.path.join(cdict['outdir'], 'imgs')

if os.path.exists(cdict['outdir']):
    msg = ("\nOutput folder\n\n{}\n\nalready exists!\nDo you want "
           "to overwrite? y/n: ".format(cdict['outdir']))
    overwrite = input(msg)
    if overwrite == 'y':
        [os.makedirs(os.path.join(stat_outdir, t), exist_ok=True)
         for t in stat_names]
        [os.makedirs(os.path.join(img_outdir, t), exist_ok=True)
         for t in stat_names]
    else:
        sys.exit()
else:
    [os.makedirs(os.path.join(stat_outdir, t)) for t in stat_names]
    [os.makedirs(os.path.join(img_outdir, t)) for t in stat_names]


# Set up SLURM client
nnodes = cdict['nodes']
sl_kwargs = cdict['slurm kwargs']
cluster = slurm_cluster_setup(nodes=nnodes, **sl_kwargs)
client = Client(cluster)

###############################################
#                                             #
#       1. OPEN DATA FILES AND REMAPPING      #
#                                             #
###############################################

# Loop over time resolution of input data
grid_coords = {}
grid_coords['meta data'] = {}
grid_coords['target grid'] = {}
data_dict = {}

for var in cdict['variables']:
    tres = cdict['variables'][var]['freq']
    print("\n\tVariable: {} -- Input resolution: {}".format(var, tres))
    grid_coords['meta data'][var] = {}
    grid_coords['target grid'][var] = {}
    if tres not in data_dict:
        data_dict[tres] = {var: {}}
    else:
        data_dict[tres].update({var: {}})
    var_conf = get_variable_config(cdict['variables'][var], var)

    obs_name = cdict['variables'][var]['obs']
    obs_list = [obs_name] if not isinstance(obs_name, list) else obs_name
    obs_scf = var_conf['obs_scale_factor']
    obs_scf = ([obs_scf]*len(obs_list)
               if not isinstance(obs_scf, list) else obs_scf)
    if obs_name is not None:
        for oname, cfactor in zip(obs_list, obs_scf):
            obs_data = get_obs_data(
                oname, var, cfactor, cdict['obs start year'],
                cdict['obs end year'], cdict['obs months'])
            data_dict[tres][var][oname] = obs_data

    mod_names = []
    for mod_name, settings in cdict['models'].items():
        mod_names.append(mod_name)
        mod_data = get_mod_data(settings, tres, var, var_conf['scale_factor'],
                                var_conf['prep_func'])
        data_dict[tres][var][mod_name] = mod_data
        if mod_name not in grid_coords['meta data'][var]:
            grid_coords['meta data'][var][mod_name] = {}
            get_grid_coords(mod_data['data'],
                            grid_coords['meta data'][var][mod_name])

    month_dd = {m: cdict['models'][m]['months'] for m in mod_names}
    year_dd = {m: (cdict['models'][m]['start year'],
                   cdict['models'][m]['end year']) for m in mod_names}
    if obs_list is not None:
        month_dd.update({o: cdict['obs months'] for o in obs_list})
        year_dd.update({o: (cdict['obs start year'], cdict['obs end year'])
                        for o in obs_list})

    ##################################
    #  1B REMAP DATA TO COMMON GRID  #
    ##################################
    remap_func(data_dict[tres][var], var, var_conf, mod_names, obs_list,
               grid_coords['target grid'][var])


###############################################
#                                             #
#          2. CALCULATE STATISTICS            #
#                                             #
###############################################

print("\n\t ---- Statistics ----\n")
stats_dict = {}
for stat in cdict['stats_conf']:
    vrs = cdict['stats_conf'][stat]['vars']
    vlist = list(cdict['variables'].keys()) if not vrs else vrs
    vrfrq = {v: cdict['variables'][v]['freq'] for v in vlist}
    pool = cdict['stats_conf'][stat]['pool data']
    chunk_dim = cdict['stats_conf'][stat]['chunk dimension']
    stats_dict[stat] = calc_stats(data_dict, vrfrq, stat, pool,
                                  chunk_dim, cdict['stats_conf'],
                                  cdict['regions'])


###############################################
#                                             #
#           3. WRITE DATA TO DISK             #
#                                             #
###############################################

for stat in cdict['stats_conf']:
    print("\nWriting {} to disk ...".format(stat))
    for v in stats_dict[stat]:
        thrlg = (('thr' in cdict['stats_conf'][stat]) and
                 (cdict['stats_conf'][stat]['thr'] is not None) and
                 (v in cdict['stats_conf'][stat]['thr']))
        if thrlg:
            thrval = str(cdict['stats_conf'][stat]['thr'][v])
            thrstr = "thr{}_".format(thrval.replace('.', ''))
        else:
            thrstr = ''
        tres = cdict['variables'][v]['freq']
        gridname = grid_coords['target grid'][v]['gridname']
        for m in stats_dict[stat][v]:
            time_suffix = get_month_string(month_dd[m])
            st_data = stats_dict[stat][v][m]
            save_to_disk(st_data, m, stat, stat_outdir, v, gridname,
                         year_dd[m][0], year_dd[m][1], time_suffix,
                         cdict['stats_conf'][stat], tres, thrstr,
                         cdict['regions'])


###############################################
#                                             #
#                 4. PLOTTING                 #
#                                             #
###############################################

if cdict['validation_plot']:
    print('\n\n\t*** --- Entering plot section --- ***\n')
    import validation_plots as vplot
    statnames = list(stats_dict.keys())
    for sn in statnames:
        for v in stats_dict[sn]:
            plot_dict = get_plot_dict(cdict, v, grid_coords, mod_names,
                                      cdict['variables'][v]['obs'], year_dd,
                                      month_dd, img_outdir, stat_outdir, sn)
            print("\t** Plotting: {} for {} **\n".format(sn, v))
            vplot.plot_main(plot_dict, sn)