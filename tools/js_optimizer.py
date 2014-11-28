
import os, sys, subprocess, multiprocessing, re, string, json, shutil
import shared

configuration = shared.configuration
temp_files = configuration.get_temp_files()

__rootpath__ = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
def path_from_root(*pathelems):
  return os.path.join(__rootpath__, *pathelems)

NATIVE_PASSES = set(['asm', 'asmPreciseF32', 'receiveJSON', 'emitJSON', 'eliminate', 'eliminateMemSafe', 'simplifyExpressions', 'simplifyIfs', 'optimizeFrounds', 'registerize', 'minifyNames', 'minifyLocals', 'minifyWhitespace'])

JS_OPTIMIZER = path_from_root('tools', 'js-optimizer.js')

NUM_CHUNKS_PER_CORE = 3
MIN_CHUNK_SIZE = int(os.environ.get('EMCC_JSOPT_MIN_CHUNK_SIZE') or 512*1024) # configuring this is just for debugging purposes
MAX_CHUNK_SIZE = int(os.environ.get('EMCC_JSOPT_MAX_CHUNK_SIZE') or 5*1024*1024)

WINDOWS = sys.platform.startswith('win')

DEBUG = os.environ.get('EMCC_DEBUG')

func_sig = re.compile('function ([_\w$]+)\(')
func_sig_json = re.compile('\["defun", ?"([_\w$]+)",')
import_sig = re.compile('var ([_\w$]+) *=[^;]+;')

NATIVE_OPTIMIZER = os.environ.get('EMCC_NATIVE_OPTIMIZER')

def get_native_optimizer():
  def create_optimizer():
    shared.logging.debug('building native optimizer')
    output = shared.Cache.get_path('optimizer.exe')
    shared.try_delete(output)
    errs = []
    for compiler in [shared.CLANG, 'g++', 'clang++']: # try our clang first, otherwise hope for a system compiler in the path
      shared.logging.debug('  using ' + compiler)
      out, err = subprocess.Popen([compiler,
                                   shared.path_from_root('tools', 'optimizer', 'istring.cpp'),
                                   shared.path_from_root('tools', 'optimizer', 'parser.cpp'),
                                   shared.path_from_root('tools', 'optimizer', 'simple_ast.cpp'),
                                   shared.path_from_root('tools', 'optimizer', 'optimizer.cpp'),
                                   '-O3', '-std=c++11', '-fno-exceptions', '-fno-rtti', '-o', output], stderr=subprocess.PIPE).communicate()
      # for profiling/debugging: '-g', '-fno-omit-frame-pointer'
      if os.path.exists(output): return output
      errs.append(err)
    raise Exception('failed to build native optimizer, errors from each attempt: ' + '\n=================\n'.join(errs))
  return shared.Cache.get('optimizer.exe', create_optimizer, extension='exe')

# Check if we should run a pass or set of passes natively. if a set of passes, they must all be valid to run in the native optimizer at once.
def use_native(x, source_map=False):
  if source_map: return False
  if not NATIVE_OPTIMIZER or NATIVE_OPTIMIZER == '0': return False
  if type(x) == str: return x in NATIVE_PASSES
  return len(NATIVE_PASSES.intersection(x)) == len(x) and 'asm' in x

class Minifier:
  '''
    asm.js minification support. We calculate minification of
    globals here, then pass that into the parallel js-optimizer.js runners which
    perform minification of locals.
  '''

  def __init__(self, js, js_engine):
    self.js = js
    self.js_engine = js_engine
    self.symbols_file = None

  def minify_shell(self, shell, minify_whitespace, source_map=False):
    # Run through js-optimizer.js to find and minify the global symbols
    # We send it the globals, which it parses at the proper time. JS decides how
    # to minify all global names, we receive a dictionary back, which is then
    # used by the function processors

    shell = shell.replace('0.0', '13371337') # avoid uglify doing 0.0 => 0

    # Find all globals in the JS functions code

    self.globs = [m.group(1) for m in func_sig.finditer(self.js)]
    if len(self.globs) == 0:
      self.globs = [m.group(1) for m in func_sig_json.finditer(self.js)]

    temp_file = temp_files.get('.minifyglobals.js').name
    f = open(temp_file, 'w')
    f.write(shell)
    f.write('\n')
    f.write('// EXTRA_INFO:' + json.dumps(self.serialize()))
    f.close()

    output = subprocess.Popen(self.js_engine +
        [JS_OPTIMIZER, temp_file, 'minifyGlobals', 'noPrintMetadata'] +
        (['minifyWhitespace'] if minify_whitespace else []) +
        (['--debug'] if source_map else []),
        stdout=subprocess.PIPE).communicate()[0]

    assert len(output) > 0 and not output.startswith('Assertion failed'), 'Error in js optimizer: ' + output
    #print >> sys.stderr, "minified SHELL 3333333333333333", output, "\n44444444444444444444"
    code, metadata = output.split('// EXTRA_INFO:')
    self.globs = json.loads(metadata)

    if self.symbols_file:
      mapfile = open(self.symbols_file, 'w')
      for key, value in self.globs.iteritems():
        mapfile.write(value + ':' + key + '\n')
      mapfile.close()
      print >> sys.stderr, 'wrote symbol map file to', self.symbols_file

    return code.replace('13371337', '0.0')


  def serialize(self):
    return {
      'globals': self.globs
    }

start_funcs_marker = '// EMSCRIPTEN_START_FUNCS\n'
end_funcs_marker = '// EMSCRIPTEN_END_FUNCS\n'
start_asm_marker = '// EMSCRIPTEN_START_ASM\n'
end_asm_marker = '// EMSCRIPTEN_END_ASM\n'

def run_on_chunk(command):
  try:
    if JS_OPTIMIZER in command: # XXX hackish
      index = command.index(JS_OPTIMIZER)
      filename = command[index + 1]
    else:
      filename = command[1]
    #saved = 'save_' + os.path.basename(filename)
    #while os.path.exists(saved): saved = 'input' + str(int(saved.replace('input', '').replace('.txt', ''))+1) + '.txt'
    #print >> sys.stderr, 'running js optimizer command', ' '.join(map(lambda c: c if c != filename else saved, command))
    #shutil.copyfile(filename, os.path.join('/tmp/emscripten_temp', saved))
    output = subprocess.Popen(command, stdout=subprocess.PIPE).communicate()[0]
    assert len(output) > 0 and not output.startswith('Assertion failed'), 'Error in js optimizer: ' + output
    filename = temp_files.get(os.path.basename(filename) + '.jo.js').name
    f = open(filename, 'w')
    f.write(output)
    f.close()
    if DEBUG and not shared.WINDOWS: print >> sys.stderr, '.' # Skip debug progress indicator on Windows, since it doesn't buffer well with multiple threads printing to console.
    return filename
  except KeyboardInterrupt:
    # avoid throwing keyboard interrupts from a child process
    raise Exception()

def run_on_js(filename, passes, js_engine, jcache, source_map=False, extra_info=None, just_split=False, just_concat=False):
  if isinstance(jcache, bool) and jcache: jcache = shared.JCache
  if jcache: shared.JCache.ensure()

  if type(passes) == str:
    passes = [passes]

  js = open(filename).read()
  if os.linesep != '\n':
    js = js.replace(os.linesep, '\n') # we assume \n in the splitting code

  # Find suffix
  suffix_marker = '// EMSCRIPTEN_GENERATED_FUNCTIONS'
  suffix_start = js.find(suffix_marker)
  suffix = ''
  if suffix_start >= 0:
    suffix_end = js.find('\n', suffix_start)
    suffix = js[suffix_start:suffix_end] + '\n'
    # if there is metadata, we will run only on the generated functions. If there isn't, we will run on everything.
    generated = set(eval(suffix[len(suffix_marker)+1:]))

  # Find markers
  start_funcs = js.find(start_funcs_marker)
  end_funcs = js.rfind(end_funcs_marker)

  know_generated = suffix or start_funcs >= 0

  minify_globals = 'minifyNames' in passes and 'asm' in passes
  if minify_globals:
    passes = map(lambda p: p if p != 'minifyNames' else 'minifyLocals', passes)
    start_asm = js.find(start_asm_marker)
    end_asm = js.rfind(end_asm_marker)
    assert (start_asm >= 0) == (end_asm >= 0)

  closure = 'closure' in passes
  if closure:
    passes = filter(lambda p: p != 'closure', passes) # we will do it manually

  cleanup = 'cleanup' in passes
  if cleanup:
    passes = filter(lambda p: p != 'cleanup', passes) # we will do it manually

  if not know_generated and jcache:
    # JCache cannot be used without metadata, since it might reorder stuff, and that's dangerous since only generated can be reordered
    # This means jcache does not work after closure compiler runs, for example. But you won't get much benefit from jcache with closure
    # anyhow (since closure is likely the longest part of the build).
    if DEBUG: print >>sys.stderr, 'js optimizer: no metadata, so disabling jcache'
    jcache = False

  if know_generated:
    if not minify_globals:
      pre = js[:start_funcs + len(start_funcs_marker)]
      post = js[end_funcs + len(end_funcs_marker):]
      js = js[start_funcs + len(start_funcs_marker):end_funcs]
      if 'asm' not in passes: # can have Module[..] and inlining prevention code, push those to post
        class Finals:
          buf = []
        def process(line):
          if len(line) > 0 and (line.startswith(('Module[', 'if (globalScope)')) or line.endswith('["X"]=1;')):
            Finals.buf.append(line)
            return False
          return True
        js = '\n'.join(filter(process, js.split('\n')))
        post = '\n'.join(Finals.buf) + '\n' + post
      post = end_funcs_marker + post
    else:
      # We need to split out the asm shell as well, for minification
      pre = js[:start_asm + len(start_asm_marker)]
      post = js[end_asm:]
      asm_shell = js[start_asm + len(start_asm_marker):start_funcs + len(start_funcs_marker)] + '''
EMSCRIPTEN_FUNCS();
''' + js[end_funcs + len(end_funcs_marker):end_asm + len(end_asm_marker)]
      js = js[start_funcs + len(start_funcs_marker):end_funcs]

      # we assume there is a maximum of one new name per line
      minifier = Minifier(js, js_engine)
      def check_symbol_mapping(p):
        if p.startswith('symbolMap='):
          minifier.symbols_file = p.split('=')[1]
          return False
        return True
      passes = filter(check_symbol_mapping, passes)
      asm_shell_pre, asm_shell_post = minifier.minify_shell(asm_shell, 'minifyWhitespace' in passes, source_map).split('EMSCRIPTEN_FUNCS();');
      asm_shell_post = asm_shell_post.replace('});', '})');
      pre += asm_shell_pre + '\n' + start_funcs_marker
      post = end_funcs_marker + asm_shell_post + post

      minify_info = minifier.serialize()
      #if DEBUG: print >> sys.stderr, 'minify info:', minify_info
    # remove suffix if no longer needed
    if suffix and 'last' in passes:
      suffix_start = post.find(suffix_marker)
      suffix_end = post.find('\n', suffix_start)
      post = post[:suffix_start] + post[suffix_end:]

  else:
    pre = ''
    post = ''

  def split_funcs(js, just_split=False):
    if just_split: return map(lambda line: ('(json)', line), js.split('\n'))
    # Pick where to split into chunks, so that (1) they do not oom in node/uglify, and (2) we can run them in parallel
    # If we have metadata, we split only the generated code, and save the pre and post on the side (and do not optimize them)
    parts = map(lambda part: part, js.split('\n}\n'))
    funcs = []
    for i in range(len(parts)):
      func = parts[i]
      if i < len(parts)-1: func += '\n}\n' # last part needs no }
      m = func_sig.search(func)
      if m:
        ident = m.group(1)
      else:
        if know_generated: continue # ignore whitespace
        ident = 'anon_%d' % i
      assert ident
      funcs.append((ident, func))
    return funcs

  total_size = len(js)
  funcs = split_funcs(js, just_split)
  js = None

  # if we are making source maps, we want our debug numbering to start from the
  # top of the file, so avoid breaking the JS into chunks
  cores = 1 if source_map else int(os.environ.get('EMCC_CORES') or multiprocessing.cpu_count())

  if not just_split:
    intended_num_chunks = int(round(cores * NUM_CHUNKS_PER_CORE))
    chunk_size = min(MAX_CHUNK_SIZE, max(MIN_CHUNK_SIZE, total_size / intended_num_chunks))
    chunks = shared.chunkify(funcs, chunk_size, jcache.get_cachename('jsopt') if jcache else None)
  else:
    # keep same chunks as before
    chunks = map(lambda f: f[1], funcs)

  chunks = filter(lambda chunk: len(chunk) > 0, chunks)
  if DEBUG and len(chunks) > 0: print >> sys.stderr, 'chunkification: num funcs:', len(funcs), 'actual num chunks:', len(chunks), 'chunk size range:', max(map(len, chunks)), '-', min(map(len, chunks))
  funcs = None

  if jcache:
    # load chunks from cache where we can # TODO: ignore small chunks
    cached_outputs = []
    def load_from_cache(chunk):
      keys = [chunk]
      shortkey = shared.JCache.get_shortkey(keys) # TODO: share shortkeys with later code
      out = shared.JCache.get(shortkey, keys)
      if out:
        cached_outputs.append(out)
        return False
      return True
    chunks = filter(load_from_cache, chunks)
    if len(cached_outputs) > 0:
      if DEBUG: print >> sys.stderr, '  loading %d jsfuncchunks from jcache' % len(cached_outputs)
    else:
      cached_outputs = []

  if len(chunks) > 0:
    def write_chunk(chunk, i):
      temp_file = temp_files.get('.jsfunc_%d.js' % i).name
      f = open(temp_file, 'w')
      f.write(chunk)
      f.write(suffix_marker)
      if minify_globals:
        if extra_info:
          for key, value in extra_info.iteritems():
            assert key not in minify_info or value == minify_info[key], [key, value, minify_info[key]]
            minify_info[key] = value
        f.write('\n')
        f.write('// EXTRA_INFO:' + json.dumps(minify_info))
      elif extra_info:
        f.write('\n')
        f.write('// EXTRA_INFO:' + json.dumps(extra_info))
      f.close()
      return temp_file
    filenames = [write_chunk(chunks[i], i) for i in range(len(chunks))]
  else:
    filenames = []

  if len(filenames) > 0:
    if not use_native(passes, source_map) or 'receiveJSON' not in passes or 'emitJSON' not in passes:
      commands = map(lambda filename: js_engine +
          [JS_OPTIMIZER, filename, 'noPrintMetadata'] +
          (['--debug'] if source_map else []) + passes, filenames)
    else:
      # use the native optimizer
      shared.logging.debug('js optimizer using native')
      assert not source_map # XXX need to use js optimizer
      commands = map(lambda filename: [get_native_optimizer(), filename] + passes, filenames)
    #print [' '.join(command) for command in commands]

    cores = min(cores, len(filenames))
    if len(chunks) > 1 and cores >= 2:
      # We can parallelize
      if DEBUG: print >> sys.stderr, 'splitting up js optimization into %d chunks, using %d cores  (total: %.2f MB)' % (len(chunks), cores, total_size/(1024*1024.))
      pool = multiprocessing.Pool(processes=cores)
      filenames = pool.map(run_on_chunk, commands, chunksize=1)
    else:
      # We can't parallize, but still break into chunks to avoid uglify/node memory issues
      if len(chunks) > 1 and DEBUG: print >> sys.stderr, 'splitting up js optimization into %d chunks' % (len(chunks))
      filenames = [run_on_chunk(command) for command in commands]
  else:
    filenames = []

  for filename in filenames: temp_files.note(filename)

  if closure or cleanup:
    # run on the shell code, everything but what we js-optimize
    start_asm = '// EMSCRIPTEN_START_ASM\n'
    end_asm = '// EMSCRIPTEN_END_ASM\n'
    cl_sep = 'wakaUnknownBefore(); var asm=wakaUnknownAfter(global,env,buffer)\n'

    cle = temp_files.get('.cl.js').name
    c = open(cle, 'w')
    pre_1, pre_2 = pre.split(start_asm)
    post_1, post_2 = post.split(end_asm)
    c.write(pre_1)
    c.write(cl_sep)
    c.write(post_2)
    c.close()
    if closure:
      if DEBUG: print >> sys.stderr, 'running closure on shell code'
      cld = shared.Building.closure_compiler(cle, pretty='minifyWhitespace' not in passes)
    else:
      if DEBUG: print >> sys.stderr, 'running cleanup on shell code'
      cld = cle + '.js'
      subprocess.Popen(js_engine + [JS_OPTIMIZER, cle, 'noPrintMetadata'] + (['minifyWhitespace'] if 'minifyWhitespace' in passes else []), stdout=open(cld, 'w')).communicate()
    temp_files.note(cld)
    coutput = open(cld).read()
    coutput = coutput.replace('wakaUnknownBefore();', start_asm)
    after = 'wakaUnknownAfter'
    start = coutput.find(after)
    end = coutput.find(')', start)
    pre = coutput[:start] + '(function(global,env,buffer) {\n' + pre_2[pre_2.find('{')+1:]
    post = post_1 + end_asm + coutput[end+1:]

  filename += '.jo.js'
  f = open(filename, 'w')
  f.write(pre);
  pre = None

  if not just_concat:
    # sort functions by size, to make diffing easier and to improve aot times
    funcses = []
    for out_file in filenames:
      funcses.append(split_funcs(open(out_file).read()))
    funcs = [item for sublist in funcses for item in sublist]
    funcses = None
    def sorter(x, y):
      diff = len(y[1]) - len(x[1])
      if diff != 0: return diff
      if x[0] < y[0]: return 1
      elif x[0] > y[0]: return -1
      return 0
    funcs.sort(sorter)

    if 'last' in passes and len(funcs) > 0:
      count = funcs[0][1].count('\n')
      if count > 3000:
        print >> sys.stderr, 'warning: Output contains some very large functions (%s lines in %s), consider building source files with -Os or -Oz, and/or trying OUTLINING_LIMIT to break them up (see settings.js; note that the parameter there affects AST nodes, while we measure lines here, so the two may not match up)' % (count, funcs[0][0])

    for func in funcs:
      f.write(func[1])
    funcs = None
  else:
    # just concat the outputs
    for out_file in filenames:
      f.write(open(out_file).read())
    assert not jcache
  f.write('\n')
  if jcache:
    for cached in cached_outputs:
      f.write(cached); # TODO: preserve order
      f.write('\n')
  f.write(post);
  # No need to write suffix: if there was one, it is inside post which exists when suffix is there
  f.write('\n')
  f.close()

  if jcache:
    # save chunks to cache
    for i in range(len(chunks)):
      chunk = chunks[i]
      keys = [chunk]
      shortkey = shared.JCache.get_shortkey(keys)
      shared.JCache.set(shortkey, keys, open(filenames[i]).read())
    if DEBUG and len(chunks) > 0: print >> sys.stderr, '  saving %d jsfuncchunks to jcache' % len(chunks)

  return filename

def run(filename, passes, js_engine=shared.NODE_JS, jcache=False, source_map=False, extra_info=None, just_split=False, just_concat=False):
  if 'receiveJSON' in passes: just_split = True
  if 'emitJSON' in passes: just_concat = True
  js_engine = shared.listify(js_engine)
  return temp_files.run_and_clean(lambda: run_on_js(filename, passes, js_engine, jcache, source_map, extra_info, just_split, just_concat))

if __name__ == '__main__':
  last = sys.argv[-1]
  if '{' in last:
    extra_info = json.loads(last)
    sys.argv = sys.argv[:-1]
  else:
    extra_info = None
  out = run(sys.argv[1], sys.argv[2:], extra_info=extra_info)
  shutil.copyfile(out, sys.argv[1] + '.jsopt.js')

