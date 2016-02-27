'''
Tries to evaluate global constructors, applying their effects ahead of time.

This is an LTO-like operation, and to avoid parsing the entire tree (we might fail to parse a massive project, we operate on the text in python.
'''

import os, sys, json, subprocess
import shared, js_optimizer

js_file = sys.argv[1]
mem_init_file = sys.argv[2]
total_memory = int(sys.argv[3])
total_stack = int(sys.argv[4])
global_base = int(sys.argv[5])

assert global_base > 0

temp_file = js_file + '.ctorEval.js'
if shared.DEBUG:
  temp_file = '/tmp/emscripten_temp/ctorEval.js'

# helpers

def get_asm(js):
  return js[js.find(js_optimizer.start_asm_marker):js.find(js_optimizer.end_asm_marker)]

def find_ctors(js):
  ctors_start = js.find('__ATINIT__.push(')
  if ctors_start < 0:
    return (-1, -1)
  ctors_end = js.find(');', ctors_start)
  assert ctors_end > 0
  ctors_end += 3
  return (ctors_start, ctors_end)

def eval_ctors(js, mem_init, num):

  def kill_func(asm, name):
    before = len(asm)
    asm = asm.replace('function ' + name + '(', 'function KILLED_' + name + '(', 1)
    return asm

  def add_func(asm, func):
    before = len(asm)
    asm = asm.replace('function ', ' ' + func + '\nfunction ', 1)
    assert len(asm) > before
    name = func[func.find(' ')+1 : func.find('(')]
    asm = asm.replace('return {', 'return { ' + name + ': ' + name + ',')
    return asm

  # Find the global ctors
  ctors_start, ctors_end = find_ctors(js)
  assert ctors_start > 0
  ctors_text = js[ctors_start:ctors_end]
  all_ctors = filter(lambda ctor: ctor.endswith('()') and not ctor == 'function()' and '.' not in ctor, ctors_text.split(' '))
  all_ctors = map(lambda ctor: ctor.replace('()', ''), all_ctors)
  total_ctors = len(all_ctors)
  assert total_ctors > 0
  ctors = all_ctors[:num]
  shared.logging.debug('trying to eval ctors: ' + ', '.join(ctors))
  # Find the asm module, and receive the mem init.
  asm = get_asm(js)
  assert len(asm) > 0
  asm = asm.replace('use asm', 'not asm') # don't try to validate this
  # find all global vars, and provide only safe ones. Also add dumping for those.
  pre_funcs_start = asm.find(';') + 1
  pre_funcs_end = asm.find('function ', pre_funcs_start)
  pre_funcs_end = asm.rfind(';', pre_funcs_start, pre_funcs_end) + 1
  pre_funcs = asm[pre_funcs_start:pre_funcs_end]
  parts = filter(lambda x: x.startswith('var '), map(lambda x: x.strip(), pre_funcs.split(';')))
  global_vars = []
  new_globals = '\n'
  for part in parts:
    part = part[4:] # skip 'var '
    bits = map(lambda x: x.strip(), part.split(','))
    for bit in bits:
      name, value = map(lambda x: x.strip(), bit.split('='))
      if value in ['0', '+0', '0.0'] or name in [
        'STACKTOP', 'STACK_MAX', 'DYNAMICTOP',
        'HEAP8', 'HEAP16', 'HEAP32',
        'HEAPU8', 'HEAPU16', 'HEAPU32',
        'HEAPF32', 'HEAPF64',
        'nan', 'inf',
        '_emscripten_memcpy_big',
      ] or name.startswith('Math_'):
        if 'new ' not in value:
          global_vars.append(name)
        new_globals += ' var ' + name + ' = ' + value + ';\n'
  asm = asm[:pre_funcs_start] + new_globals + asm[pre_funcs_end:]
  asm = add_func(asm, 'function dumpGlobals() { return [ ' + ', '.join(global_vars) + '] }')
  # find static bump. this is the maximum area we'll write to during startup.
  static_bump_op = 'STATICTOP = STATIC_BASE + '
  static_bump_start = js.find(static_bump_op)
  static_bump_end = js.find(';', static_bump_start)
  static_bump = int(js[static_bump_start + len(static_bump_op):static_bump_end])
  # remove malloc/free, if present, and add a simple malloc that adds to the mem init file.
  # this makes mallocs evallable, and avoids malloc allocator fragmentation, etc. However,
  # if we see a bunch of frees, we give up
  asm = kill_func(asm, '_malloc')
  asm = kill_func(asm, '_free')
  asm = add_func(asm, '''
function _malloc(x) {
  if (x === 0) x = 1;
  while (staticTop % 16 !== 0) staticTop++;
  if (staticTop >= stackBase) throw 'not enough room for an allocation of size ' + x;
  var ret = staticTop;
  staticTop += x;
  addSegment(ret, x);
  return ret;
}
''')
  asm = add_func(asm, '''
function _free(x) {
  if (x) freeSegment(x);
}
''')
  # Generate a safe sandboxed environment. We replace all ffis with errors. Otherwise,
  # asm.js can't call outside, so we are ok.
  open(temp_file, 'w').write('''
var totalMemory = %d;
var totalStack = %d;

var buffer = new ArrayBuffer(totalMemory);
var heap = new Uint8Array(buffer);

var memInit = %s;

var globalBase = %d;
var staticBump = %d;

heap.set(memInit, globalBase);

var staticTop = globalBase + staticBump;
var staticBase = staticTop;

var stackTop = totalMemory - totalStack; // put it anywhere - it's not memory we need after this execution (we ensure stack is unwound)
while (stackTop %% 16 !== 0) stackTop--;
if (stackTop <= staticTop) throw 'not enough room for stack';
var stackBase = stackTop;

var stackMax = stackTop + totalStack;
var dynamicTop = stackMax;

// malloc manangement

var segments = []; // list of malloc segments

function optimizeSegments() {
  while (1) {
    var more = false;
    for (var i = 0; i < segments.length - 1; i++) {
      if (segments[i].free && segments[i+1].free && segments[i].start === segments[i+1].end) {
        segments[i].start = segments[i+1].end;
        segments.splice(i+1, 1);
        i--;
        more = true;
      }
    }
    if (!more) break;
  }
  if (segments.length > 0) {
    staticTop = segments[segments.length-1].end;
  }
}
function addSegment(ptr, size) {
  segments.push({ start: ptr, end: ptr + size, free: false }); // always at the end
}
function freeSegment(ptr) {
  for (var i = 0; i < segments.length; i++) {
    if (segments[i].start === ptr) {
      segments[i].free = true;
      optimizeSegments();
      return;
    }
  }
  // ignore a bad free()
}
function calculateWastedSegments() {
  var waste = 0;
  for (var i = 0; i < segments.length; i++) {
    if (segments[i].free) {
      waste += segments[i].end - segments[i].start;
    }
  }
  return waste;
}
// end malloc management

if (!Math.imul) {
  Math.imul = Math.imul || function(a, b) {
    var ah = (a >>> 16) & 0xffff;
    var al = a & 0xffff;
    var bh = (b >>> 16) & 0xffff;
    var bl = b & 0xffff;
    // the shift by 0 fixes the sign on the high part
    // the final |0 converts the unsigned value into a signed value
    return ((al * bl) + (((ah * bl + al * bh) << 16) >>> 0)|0);
  };
}
if (!Math.fround) {
  var froundBuffer = new Float32Array(1);
  Math.fround = function(x) { froundBuffer[0] = x; return froundBuffer[0] };
}

var globalArg = {
  Int8Array: Int8Array,
  Int16Array: Int16Array,
  Int32Array: Int32Array,
  Uint8Array: Uint8Array,
  Uint16Array: Uint16Array,
  Uint32Array: Uint32Array,
  Float32Array: Float32Array,
  Float64Array: Float64Array,
  NaN: NaN,
  Infinity: Infinity,
  Math: Math,
};

var libraryArg = {
  STACKTOP: stackTop,
  STACK_MAX: stackMax,
  DYNAMICTOP: dynamicTop,
  _emscripten_memcpy_big: function(dest, src, num) {
    heap.set(heap.subarray(src, src+num), dest);
    return dest;
  },
};

// Instantiate asm
%s
(globalArg, libraryArg, buffer);

var globalsBefore = asm['dumpGlobals']();

// Try to run the constructors
(%s).forEach(function(ctor) {
  asm[ctor]();
});
// We succeeded!

// Verify asm global vars
var globalsAfter = asm['dumpGlobals']();

if (JSON.stringify(globalsBefore) !== JSON.stringify(globalsAfter)) throw 'globals changed ' + globalsBefore + ' vs ' + globalsAfter;

// Check if malloc/free is leading to too much waste
var waste = calculateWastedSegments();
if (waste > 1024 && waste > 0.25 * staticBump) throw 'too much waste caused by free()s'; // XXX FIXME 1 percent of totalMemory

// Write out new mem init. It might be bigger if we added to the zero section; mallocs might make it even bigger than the original staticBump.
var newSize;
if (staticTop > staticBase) {
  // we malloced
  newSize = staticTop;
} else {
  // look for zeros
  newSize = globalBase + staticBump;
  while (newSize > globalBase && heap[newSize-1] == 0) newSize--;
}
console.log(Array.prototype.slice.call(heap.subarray(globalBase, newSize)));

''' % (total_memory, total_stack, mem_init, global_base, static_bump, asm, json.dumps(ctors)))
  # Execute the sandboxed code. If an error happened due to calling an ffi, that's fine,
  # us exiting with an error tells the caller that we failed.
  proc = subprocess.Popen(shared.NODE_JS + [temp_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  out, err = proc.communicate()
  if proc.returncode != 0:
    shared.logging.debug('failed to eval ctors:\n' + err)
    if '_atexit' in err:
      shared.logging.debug('note: consider using  -s NO_EXIT_RUNTIME=1  to maximize the effectiveness of EVAL_CTORS')
    return False
  # Success! out contains the new mem init, write it out
  mem_init = ''.join(map(chr, json.loads(out)))
  # Remove this ctor and write that out
  if len(ctors) == total_ctors:
    new_ctors = '' # remove them all
  else:
    temp = ctors_text.find(',') + 1
    for i in range(len(ctors)-1):
      temp = ctors_text.find(',', temp) + 1
    new_ctors = ctors_text[:ctors_text.find('(') + 1] + ctors_text[temp:]
  js = js[:ctors_start] + new_ctors + js[ctors_end:]
  if len(mem_init) > static_bump:
    # we malloced, and need a bigger mem init
    static_bump_action = 'STATICTOP = STATIC_BASE + %d;' % static_bump
    assert js.count(static_bump_action) == 1
    size = len(mem_init)
    while size % 16 != 0: size += 1
    js = js.replace(static_bump_action, 'STATICTOP = STATIC_BASE + %d;' % size)
  return (js, mem_init, ctors)

# main

js = open(js_file).read()
ctors_start, ctors_end = find_ctors(js)
if ctors_start < 0:
  shared.logging.debug('ctor_evaller: no ctors')
  sys.exit(0)

num_ctors = js[ctors_start:ctors_end].count(',') + 1
shared.logging.debug('ctor_evaller: %d ctors' % num_ctors)

if os.path.exists(mem_init_file):
  mem_init = json.dumps(map(ord, open(mem_init_file, 'rb').read()))
else:
  mem_init = []

# find how many ctors we can remove, by bisection (if there are hundreds, running them sequentially is silly slow)

low = 0 # definitely possible; will remain a valid value
high = num_ctors + 1 # definitely impossible; will remain an invalid value
next = num_ctors # be optimistic, try all of them to begin with

while True:
  shared.logging.debug('ctor_evaller: trying to eval %d global constructors' % next)
  result = eval_ctors(js, mem_init, next)
  if not result:
    shared.logging.debug('ctor_evaller: not successful')
    if next == low + 1:
      shared.logging.debug('ctor_evaller: done')
      break
    high = next
    next = (low + next) / 2
    continue
  shared.logging.debug('ctor_evaller: success!')
  low = next
  if next == high - 1:
    shared.logging.debug('ctor_evaller: done')
    break
  next = (next + high) / 2

if low == 0:
  sys.exit(0) # we failed to remove even one

# final execution of optimal result
shared.logging.debug('ctor_evaller: we managed to remove %d ctors' % low)
js, mem_init, removed = eval_ctors(js, mem_init, low)
open(js_file, 'w').write(js)
open(mem_init_file, 'wb').write(mem_init)

# Dead function elimination can help us

shared.logging.debug('ctor_evaller: eliminate no longer needed functions after ctor elimination')
# find exports
asm = get_asm(open(js_file).read())
exports_start = asm.find('return {')
exports_end = asm.find('};', exports_start)
exports_text = asm[asm.find('{', exports_start) + 1 : exports_end]
exports = map(lambda x: x.split(':')[1].strip(), exports_text.replace(' ', '').split(','))
for r in removed:
  assert r in exports, 'global ctors were exported'
exports = filter(lambda e: e not in removed, exports)
# fix up the exports
js = open(js_file).read()
absolute_exports_start = js.find(exports_text)
js = js[:absolute_exports_start] + ', '.join(map(lambda e: e + ': ' + e, exports)) + js[absolute_exports_start + len(exports_text):]
open(js_file, 'w').write(js)
# find unreachable methods and remove them
reachable = shared.Building.calculate_reachable_functions(js_file, exports, can_reach=False)['reachable']
for r in removed:
  assert r not in reachable, 'removed ctors must NOT be reachable'
shared.Building.js_optimizer(js_file, ['removeFuncs'], extra_info={ 'keep': reachable }, output_filename=js_file)

