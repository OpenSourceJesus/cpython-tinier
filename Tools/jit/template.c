/* JIT micro-op stencil template, compiled by ShivyC (no LLVM).
 *
 * This is the body of every JIT-compiled trace in the ShivyC proof of concept.
 * When a trace is entered it computes and returns the tier-1 resume target
 * (frame->instr_ptr); the C shim (shim.c::_PyJIT_Entry) then detaches the
 * executor so the interpreter resumes with forward progress.
 *
 * It is deliberately written in the small subset of C that ShivyC supports:
 * pure pointer arithmetic, no CPython headers, no floating point. That keeps
 * the generated code fully position-independent so jit.c can memcpy it into
 * executable memory and run it directly.
 *
 * build.py prepends a definition of _JIT_INSTR_PTR_OFFSET (the byte offset of
 * _PyInterpreterFrame.instr_ptr, discovered at build time) before compiling.
 *
 * The 7-argument signature matches jit_func from pycore_jit.h:
 *   _Py_CODEUNIT *(*)(executor, frame, stack_pointer, tstate,
 *                     tos_cache0, tos_cache1, tos_cache2)
 */

typedef long word;

word *_jit_deopt(word *executor, word *frame, word *stack_pointer, word *tstate,
                 word tos0, word tos1, word tos2) {
    return *(word **)((char *)frame + _JIT_INSTR_PTR_OFFSET);
}
