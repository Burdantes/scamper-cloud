# Generic container v1

This is the original single-target-file container used by the early
`scamperctl deploy` workflow. It is retained for reproducibility but is not a
supported trace/RR experiment contract: it does not model the two target
populations independently or enforce decoded cardinality. New images must use
the definitions under `experiments/`.
