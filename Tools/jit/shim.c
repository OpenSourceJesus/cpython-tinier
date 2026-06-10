/* JIT entry shim, compiled by the host C compiler (no LLVM).
 *
 * _PyJIT_Entry is the trampoline tier-1 calls to enter a JIT-compiled trace
 * (see TIER1_TO_TIER2 in Python/ceval_macros.h). It invokes the ShivyC-
 * generated stencil (executor->jit_code) to obtain the resume target, then
 * detaches the executor so the original bytecode is restored at the trace head
 * and the tier-1 interpreter makes forward progress -- guaranteeing no
 * infinite re-entry while keeping execution semantics correct.
 *
 * Unlike the stencils, this needs real CPython internals (jit_func, the frame
 * accessors, _Py_ExecutorDetach), so it is compiled by the same host compiler
 * that builds the rest of CPython. It still pulls in no LLVM/clang.
 */

#include "Python.h"
#include "pycore_jit.h"
#include "pycore_frame.h"
#include "pycore_interpframe.h"
#include "pycore_optimizer.h"
#include "pycore_stackref.h"

#ifdef _Py_JIT

_Py_CODEUNIT *
_PyJIT_Entry(_PyExecutorObject *executor, _PyInterpreterFrame *frame,
             _PyStackRef *stack_pointer, PyThreadState *tstate)
{
    jit_func code = (jit_func)executor->jit_code;
    _Py_CODEUNIT *target = code(executor, frame, stack_pointer, tstate,
                                PyStackRef_ZERO_BITS, PyStackRef_ZERO_BITS,
                                PyStackRef_ZERO_BITS);
    tstate->current_executor = NULL;
    _PyFrame_SetStackPointer(frame, stack_pointer);
    _Py_ExecutorDetach(executor);
    return target;
}

#endif  /* _Py_JIT */
