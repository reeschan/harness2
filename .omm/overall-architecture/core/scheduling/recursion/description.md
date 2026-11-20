`harness2/core/recursion.py`: parallel and sequential task-level recursion. Each mode schedules
R = 4k rollouts and P = 3k improvement stages, including the shared base executions; an invalid
proposal gets one retry before the current harness is inherited.
