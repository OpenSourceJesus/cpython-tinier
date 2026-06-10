/* Stencil ABI shared between the generated jit_stencils header and Python/jit.c.
 *
 * build.py inlines this fragment into the generated jit_stencils-<triple>.h so
 * that Python/jit.c (which #includes "jit_stencils.h") sees the StencilGroup
 * layout it expects: a code/data size, an emit callback, and the trampoline /
 * GOT symbol masks. The fragment is intentionally include-guard-free because it
 * is concatenated, not #included.
 *
 * Types referenced here (_PyExecutorObject, _PyUOpInstruction, jit_state,
 * symbol_mask) are all already in scope at the point Python/jit.c includes the
 * generated header.
 */

typedef void (*emit_func)(
    unsigned char *code, unsigned char *data,
    _PyExecutorObject *executor, const _PyUOpInstruction *instruction,
    jit_state *state);

typedef struct {
    size_t code_size;
    size_t data_size;
    emit_func emit;
    symbol_mask trampoline_mask;
    symbol_mask got_mask;
} StencilGroup;
