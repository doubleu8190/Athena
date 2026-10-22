UPDATE agent_tasks SET status = 'completed' WHERE status = 'done';
UPDATE agent_tasks SET status = 'failed' WHERE status = 'timed_out';

UPDATE round_tasks SET status = 'completed' WHERE status = 'done';
UPDATE round_tasks SET status = 'failed' WHERE status = 'timed_out';

UPDATE task_executions SET status = 'completed' WHERE status = 'done';
UPDATE task_executions SET status = 'failed' WHERE status = 'timed_out';
