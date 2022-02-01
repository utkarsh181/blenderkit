import asyncio
import sys
import os
import aiohttp
import tempfile
import json
import subprocess
import uuid
import shutil
import certifi
import ssl
import re

from aiohttp import web

PORT = 8080

resolutions = {
  'resolution_0_5K': 512,
  'resolution_1K': 1024,
  'resolution_2K': 2048,
  'resolution_4K': 4096,
  'resolution_8K': 8192,
}

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
HIGH_PRIORITY_CLASS = 0x00000080
IDLE_PRIORITY_CLASS = 0x00000040
NORMAL_PRIORITY_CLASS = 0x00000020
REALTIME_PRIORITY_CLASS = 0x00000100

def get_process_flags():
  '''
  Gets proper priority flags so background processess can run with lower priority
  '''
  flags = BELOW_NORMAL_PRIORITY_CLASS
  if sys.platform != 'win32':  # TODO test this on windows
    flags = 0
  return flags


def get_res_file(data, find_closest_with_url=False):  # asset_data, resolution, find_closest_with_url=False):
  '''
  Returns closest resolution that current asset can offer.
  If there are no resolutions, return orig file.
  If orig file is requested, return it.
  params
  asset_data
  resolution - ideal resolution
  find_closest_with_url:
      returns only resolutions that already containt url in the asset data, used in scenes where asset is/was already present.
  Returns:
      resolution file
      resolution, so that other processess can pass correctly which resolution is downloaded.
  '''
  orig = None
  res = None
  closest = None
  target_resolution = resolutions.get(data['resolution'])
  mindist = 100000000

  for f in data['asset_data']['files']:
    if f['fileType'] == 'blend':
      orig = f
      if data['resolution'] == 'blend':
        # orig file found, return.
        return orig, 'blend'

    if f['fileType'] == data['resolution']:
      # exact match found, return.
      return f, data['resolution']
    # find closest resolution if the exact match won't be found.
    rval = resolutions.get(f['fileType'])
    if rval and target_resolution:
      rdiff = abs(target_resolution - rval)
      if rdiff < mindist:
        closest = f
        mindist = rdiff
        # print('\n\n\n\n\n\n\n\n')
        # print(closest)
        # print('\n\n\n\n\n\n\n\n')
  if not res and not closest:
    # utils.pprint(f'will download blend instead of resolution {resolution}')
    return orig, 'blend'
  # utils.pprint(f'found closest resolution {closest["fileType"]} instead of the requested {resolution}')
  return closest, closest['fileType']

async def do_asset_download(data):
  '''
  Does download of an asset from BlenderKit:
  1. creates a Connector and Session for download, handles SSL configuration
  2. gets download URL for an asset
  3. checks whether asset exists locally
  4. gets file_path for the file
  5. downloads the file
  6. unpacks the file
  '''
  report_download_progress(data, progress=0, text='Looking for asset')
  await asyncio.sleep(.01)
  # tcom.report = 'Looking for asset'

  sslcontext = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
  sslcontext.load_verify_locations(certifi.where())
  async with aiohttp.TCPConnector(ssl=sslcontext) as conn:
    async with aiohttp.ClientSession(connector=conn) as session:
      # TODO get real link here...
      await get_download_url(data, session)  # asset_data, scene_id, api_key, resolution=self.resolution, tcom=tcom)
      # if not has_url:
      #   tasks_queue.add_task(
      #     (reports.add_report, ('Failed to obtain download URL for %s.' % asset_data['name'], 5, colors.RED)))
      #   return;
      # if tcom.error:
      #   return
      # only now we can check if the file already exists. This should have 2 levels, for materials and for brushes
      # different than for the non free content. delete is here when called after failed append tries.

      # This check happens only after get_download_url becase we need it to know what is the file name on hard drive.
      if await check_existing(data):  # and not tcom.passargs.get('delete'):
        # this sends the thread for processing, where another check should occur, since the file might be corrupted.
        # tcom.downloaded = 100
        # bk_logger.debug('not downloading, trying to append ')
        report_download_progress(data, progress=100, text='Asset found on hard drive')
        report_download_finished(data)
        print('found on hard drive, finishing ')
        return

      file_path = get_download_filepaths(data)[0]
      # prefer global dir if possible.
      # for k in asset_data:
      #    print(asset_data[k])
      # if self.stopped():
      #   bk_logger.debug('stopping download: ' + asset_data['name'])
      #   return
      await download_file(session, file_path, data)
      # unpack the file immediately after download

      report_download_progress(data, progress=100, text='Unpacking files')
      await asyncio.sleep(.01)
      # TODO: check if resolution is written correctly into assetdata hanging on actual appended object in scene and probably
      # remove the following line?
      data['asset_data']['resolution'] = data['resolution']
      await send_to_bg(data, file_path, command='unpack')

      # print(f'Finished asset download: {data}')
      report_download_finished(data)

async def download_file(session, file_path, data):
  print("DOWNLOADING FILE_PATH:", file_path)

  with open(file_path, "wb") as file:
    res_file_info, data['resolution'] = get_res_file(data)     
    async with session.get(res_file_info['url']) as resp:
      total_length = resp.headers.get('Content-Length')
      if total_length is None:  # no content length header
        print('no content length: ', resp.content)
        # tcom.report = response.content
        delete_unfinished_file(file_path)
        return
      
      # bk_logger.debug(total_length)
      # if int(total_length) < 1000:  # means probably no file returned.
      # tasks_queue.add_task((reports.add_report, (response.content, 20, colors.RED)))
      #
      #   tcom.report = response.content
      file_size = int(total_length)
      fsmb = file_size // (1024 * 1024)
      fskb = file_size % 1024
      if fsmb == 0:
        t = '%iKB' % fskb
      else:
        t = ' %iMB' % fsmb
      # tcom.report = f'Downloading {t} {self.resolution}'
      report_download_progress(data, text = f"Downloading {t} {data['resolution']}", progress=0)
      downloaded = 0

      async for chunk in resp.content.iter_chunked(4096 * 32):
        # for rdata in response.iter_content(chunk_size=4096 * 32):  # crashed here... why? investigate:
        downloaded += len(chunk)
        progress = int(100 * downloaded / file_size)
        report_download_progress(data, progress=progress)
        file.write(chunk)

        if tasks[data['task_id']].get('kill'):
          delete_unfinished_file(file_path)
          return

def get_headers(api_key) -> dict[str, str]:
  '''
  Get headers with authorization.
  '''
  headers = {
    "accept": "application/json",
  }
  if api_key != '':
    headers["Authorization"] = f"Bearer {api_key}"
  return headers


def add_error_report(data, text=''):
  '''
  Adds error report to task results.
  '''
  # tasks[data['task_id']] = {
  tasks[data['task_id']] = {
    "app_id": data['PREFS']['app_id'],
    'type': 'error-report',
    # "followup": "blenderLib.placeAssetIntoScene()".
    'text': text,
    'timeout': 20,
  }
  print(text, tasks)


def report_download_progress(data, text=None, progress=None):
  '''
  Add download progress report to task results.
  '''
  global tasks
  tasks[data['task_id']] = {
    "app_id": data['PREFS']['app_id'],
    'type': 'download-progress',
  }

  if progress is not None:
    tasks[data['task_id']]['progress'] = progress
  if text is not None:
    tasks[data['task_id']]['text'] = text

  # print(progress, text, tasks)


def report_download_finished(data):
  '''
  Return download finished results.
  '''
  global tasks
  tasks[data['task_id']] = data
  tasks[data['task_id']].update({
    "app_id": data['PREFS']['app_id'],
    'type': 'download-finished',
  })

  print("FINISHED", tasks[data['task_id']])


async def get_download_url(data, session):  # asset_data, scene_id, api_key, tcom=None, resolution='blend'):
  '''
  Retrieves the download url. The server checks if user can download the item and returns url with a key.
  '''
  headers = get_headers(data['PREFS']['api_key'])
  req_data = {'scene_uuid': data['PREFS']['scene_id']}
  res_file_info, resolution = get_res_file(data)

  async with session.get(res_file_info['downloadUrl'], params=req_data, headers=headers) as resp:
      rtext = await resp.text()

      if resp == None:
        add_error_report(data, text='Connection Error')
        return False  # 'Connection Error'

      if resp.status < 400:
        rdata = await resp.json()
        url = rdata['filePath']
        res_file_info['url'] = url
        res_file_info['file_name'] = extract_filename_from_url(url)
        return True

      if resp.status == 403:
        report_text = 'You need Full plan to get this item.'

      if resp.status == 404:
        report_text = 'Url not found - 404.'
        # r1 = 'All materials and brushes are available for free. Only users registered to Standard plan can use all models.'

      elif resp.status >= 500:
        report_text = 'Server error'

  add_error_report(data, text=report_text)
  return False


def slugify(slug):
  """
  Normalizes string, converts to lowercase, removes non-alpha characters,
  and converts spaces to hyphens.
  """
  slug = slug.lower()

  characters = '<>:"/\\|?\*., ()#'
  for ch in characters:
    slug = slug.replace(ch, '_')
  # import re
  # slug = unicodedata.normalize('NFKD', slug)
  # slug = slug.encode('ascii', 'ignore').lower()
  slug = re.sub(r'[^a-z0-9]+.- ', '-', slug).strip('-')
  slug = re.sub(r'[-]+', '-', slug)
  slug = re.sub(r'/', '_', slug)
  slug = re.sub(r'\\\'\"', '_', slug)
  if len(slug) > 50:
    slug = slug[:50]
  return slug


def extract_filename_from_url(url):
  '''
   Extract filename from url.
  '''
  # print(url)
  if url is not None:
    imgname = url.split('/')[-1]
    imgname = imgname.split('?')[0]
    return imgname
  return ''


def server_2_local_filename(asset_data, filename):
  '''
  Convert file name on server to file name local.
  This should get replaced
  '''
  # print(filename)
  fn = filename.replace('blend_', '')
  fn = fn.replace('resolution_', '')
  # print('after replace ', fn)
  n = slugify(asset_data['name']) + '_' + fn
  return n


def get_download_filepaths(data):  # asset_data, resolution='blend', can_return_others=False):
  '''Get all possible paths of the asset and resolution. Usually global and local directory.'''
  can_return_others = False  # TODO find out what this was and check if it's still needed
  windows_path_limit = 250
  asset_data = data['asset_data']
  resolution = data['resolution']
  dirs = data['download_dirs']
  res_file, resolution = get_res_file(data, find_closest_with_url=can_return_others)
  name_slug = slugify(asset_data['name'])
  if len(name_slug) > 16:
    name_slug = name_slug[:16]
  asset_folder_name = f"{name_slug}_{asset_data['id']}"

  # utils.pprint('get download filenames ', dict(res_file))
  file_names = []

  if not res_file:
    return file_names
  # fn = asset_data['file_name'].replace('blend_', '')
  if res_file.get('url') is not None:
    # Tweak the names a bit:
    # remove resolution and blend words in names
    #
    fn = extract_filename_from_url(res_file['url'])
    n = server_2_local_filename(asset_data, fn)
    for d in dirs:
      asset_folder_path = os.path.join(d, asset_folder_name)
      print(asset_folder_path)
      if sys.platform == 'win32' and len(asset_folder_path) > windows_path_limit:
        add_error_report(data,
                         text='The path to assets is too long, '
                              'only Global folder can be used. '
                              'Move your .blend file to another '
                              'folder with shorter path to '
                              'store assets in a subfolder of your project.',
                         timeout=60)
        continue
      if not os.path.exists(asset_folder_path):
        os.makedirs(asset_folder_path)

      file_name = os.path.join(asset_folder_path, n)
      file_names.append(file_name)

  for f in file_names:
    if len(f) > windows_path_limit:
      add_error_report(data,
                       text='The path to assets is too long, '
                            'only Global folder can be used. '
                            'Move your .blend file to another '
                            'folder with shorter path to '
                            'store assets in a subfolder of your project.',
                       timeout=60)

      file_names.remove(f)
  return file_names


async def send_to_bg(data, fpath, command='generate_resolutions', wait=True):
  '''
  Send varioust task to a new blender instance that runs and closes after finishing the task.
  This function waits until the process finishes.
  The function tries to set the same bpy.app.debug_value in the instance of Blender that is run.
  
  Parameters
  ----------
  data
  fpath - file that will be processed
  command - command which should be run in background.

  Returns
  -------
  None
  '''
  process_data = {
    'fpath': fpath,
    'debug_value': data['PREFS']['debug_value'],
    'asset_data': data['asset_data'],
    'command': command,
  }
  binary_path = data['PREFS']['binary_path']
  tempdir = tempfile.mkdtemp()
  datafile = os.path.join(tempdir + 'resdata.json')
  script_path = os.path.dirname(os.path.realpath(__file__))
  with open(datafile, 'w', encoding='utf-8') as s:
    json.dump(process_data, s, ensure_ascii=False, indent=4)

  print('opening Blender instance to do processing - ', command)

  if wait:
    proc = subprocess.run([
      binary_path,
      "--background",
      "-noaudio",
      fpath,
      "--python", os.path.join(script_path, "resolutions_bg.py"),
      "--", datafile
    ], bufsize=1, stdout=sys.stdout, stdin=subprocess.PIPE, creationflags=get_process_flags())

  else:
    # TODO this should be fixed to allow multithreading.
    proc = subprocess.Popen([
      binary_path,
      "--background",
      "-noaudio",
      fpath,
      "--python", os.path.join(script_path, "resolutions_bg.py"),
      "--", datafile
    ], bufsize=1, stdout=subprocess.PIPE, stdin=subprocess.PIPE, creationflags=get_process_flags())
    return proc


async def copy_asset(fp1, fp2):
  '''
  Synchronizes the asset between folders, including it's texture subdirectories
  '''
  if 1:
    # bk_logger.debug('copy asset')
    # bk_logger.debug(fp1 + ' ' + fp2)
    if not os.path.exists(fp2):
      shutil.copyfile(fp1, fp2)
      # bk_logger.debug('copied')
    source_dir = os.path.dirname(fp1)
    target_dir = os.path.dirname(fp2)
    for subdir in os.scandir(source_dir):
      if not subdir.is_dir():
        continue
      target_subdir = os.path.join(target_dir, subdir.name)
      if os.path.exists(target_subdir):
        continue
      # bk_logger.debug(str(subdir) + ' ' + str(target_subdir))
      shutil.copytree(subdir, target_subdir)
      # bk_logger.debug('copied')

  # except Exception as e:
  #     print('BlenderKit failed to copy asset')
  #     print(fp1, fp2)
  #     print(e)


async def check_existing(data) -> bool:
  '''
  Check if the object exists on the hard drive.
  '''
  if data['asset_data'].get('files') == None:
    return False # this is because of some very old files where asset data had no files structure.

  file_paths = get_download_filepaths(data)

  # bk_logger.debug('check if file already exists' + str(file_names))
  if len(file_paths) == 2:
    # TODO this should check also for failed or running downloads.
    # If download is running, assign just the running thread. if download isn't running but the file is wrong size,
    #  delete file and restart download (or continue downoad? if possible.)
    if os.path.isfile(file_paths[0]):  # and not os.path.isfile(file_names[1])
      await copy_asset(file_paths[0], file_paths[1])
    elif not os.path.isfile(file_paths[0]) and os.path.isfile(
            file_paths[1]):  # only in case of changed settings or deleted/moved global dict.
      await copy_asset(file_paths[1], file_paths[0])

  if len(file_paths) > 0 and os.path.isfile(file_paths[0]):
    return True

  return False

def delete_unfinished_file(file_name) -> None:
    '''
    Deletes download if it wasn't finished. If the folder it's containing is empty, it also removes the directory
    '''
    try:
      os.remove(file_name)
    except Exception as e:
      print(e)
    asset_dir = os.path.dirname(file_name)
    if len(os.listdir(asset_dir)) == 0:
      os.rmdir(asset_dir)
    return

### SERVER HANDLERS ###

async def IsAlive(request):
  '''
  Reports process ID of server in Index, can be used as is-alive endpoint.
  '''
  pid = str(os.getpid())
  return web.Response(text=pid)


class KillYourself(web.View):
  '''
  Shedules kill of the server.
  '''
  async def get(self):
    asyncio.ensure_future(self.kill_in_future())
    return web.Response(text='Going to kill him soon.')

  async def kill_in_future():
    await asyncio.sleep(1)
    sys.exit()


class DownloadAsset(web.View):
  '''
  Handles downloads of assets.
  '''
  async def post(self):
    data = await self.request.json()
    print('Starting asset download:', data['asset_data']['name'])
    task_id = str(uuid.uuid4())
    data['task_id'] = task_id
    asyncio.ensure_future(do_asset_download(data))
    return web.json_response({'task_id': task_id})


class KillDownload(web.View):
  '''
  Ends download with the task_id.
  '''
  async def get(self):
    global tasks
    data = await self.request.json()
    tasks[data['task_id']]['kill'] = True


class Report(web.View):
  '''
  Reports progress of all tasks for a given app_id.
  Clears list of tasks
  '''
  async def get(self):
    global tasks
    data = await self.request.json()
    reports = {key: value for (key, value) in tasks.items() if value['app_id'] == data['app_id']}
    tasks = {key: value for (key, value) in tasks.items() if value['app_id'] != data['app_id']}
    return web.json_response(reports)


if __name__ == "__main__":
  server = web.Application()
  server.add_routes([
    web.get('/', IsAlive),
    web.view('/killyourself', KillYourself),
    web.view('/report', Report),
    web.view('/download-asset', DownloadAsset),
    web.view('/download-kill', KillDownload),
  ])

  tasks = dict()

  web.run_app(server, host='127.0.0.1', port=PORT)
