import time

import pytest

from sqs_workers import IMMEDIATE_RETURN, ExponentialBackoff
from sqs_workers.codecs import JSONCodec, PickleCodec
from sqs_workers.memory_env import MemoryEnv
from sqs_workers.processors import (
    BatchProcessor, DeadLetterProcessor, Processor)

worker_results = {'say_hello': None, 'batch_say_hello': set()}


def raise_exception(username='Anonymous'):
    raise Exception('oops')


def say_hello(username='Anonymous'):
    worker_results['say_hello'] = username


def batch_say_hello(messages):
    for msg in messages:
        worker_results['batch_say_hello'].add(msg['username'])


@pytest.fixture(autouse=True)
def _reset_worker_results():
    global worker_results
    worker_results = {'say_hello': None, 'batch_say_hello': set()}


def test_add_pickle_job(sqs, queue):
    sqs.add_job(queue, 'say_hello', username='Homer')
    job_messages = sqs.get_raw_messages(queue, 0)
    msg = job_messages[0]
    assert msg.message_attributes['JobName']['StringValue'] == 'say_hello'
    assert msg.message_attributes['ContentType']['StringValue'] == 'pickle'
    assert PickleCodec.deserialize(msg.body) == {'username': 'Homer'}


def test_add_json_job(sqs, queue):
    sqs.add_job(queue, 'say_hello', username='Homer', _content_type='json')
    job_messages = sqs.get_raw_messages(queue, 0)
    msg = job_messages[0]
    assert msg.message_attributes['JobName']['StringValue'] == 'say_hello'
    assert msg.message_attributes['ContentType']['StringValue'] == 'json'
    assert JSONCodec.deserialize(msg.body) == {'username': 'Homer'}


def test_processor(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay(username='Homer')
    assert worker_results['say_hello'] is None
    sqs.process_batch(queue, wait_seconds=0)
    assert worker_results['say_hello'] == 'Homer'


def test_process_messages_once(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay(username='Homer')
    processed = sqs.process_batch(queue, wait_seconds=0).succeeded_count()
    assert processed == 1
    processed = sqs.process_batch(queue, wait_seconds=0).succeeded_count()
    assert processed == 0


def test_batch_processor(sqs, queue):
    task = sqs.processors.connect_batch(queue, 'batch_say_hello',
                                       batch_say_hello)

    usernames = {'u{}'.format(i) for i in range(20)}

    # enqueue messages
    for username in usernames:
        task.delay(username=username)

    # sometimes SQS doesn't return all messages at once, and we need to drain
    # the queue with the infinite loop
    while True:
        processed = sqs.process_batch(queue, wait_seconds=0).succeeded_count()
        if processed == 0:
            break

    assert worker_results['batch_say_hello'] == usernames


def test_copy_processors(sqs, queue, queue2):
    # indirectly set the processor for queue2
    sqs.processors.connect(queue, 'say_hello', say_hello)
    sqs.processors.copy(queue, queue2)

    # add job to that queue
    sqs.add_job(queue2, 'say_hello')

    # and see that it's succeeded
    processed = sqs.process_batch(queue2, wait_seconds=0).succeeded_count()
    assert processed == 1


def test_arguments_validator_raises_exception_on_extra(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    with pytest.raises(TypeError):
        say_hello_task.delay(username='Homer', foo=1)


def test_arguments_validator_adds_kwargs(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay()
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 1
    assert worker_results['say_hello'] == 'Anonymous'


def test_delay_accepts_converts_args_to_kwargs(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay('Homer')  # we don't use username="Homer"
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 1
    assert worker_results['say_hello'] == 'Homer'


def test_exception_returns_task_to_the_queue(sqs, queue):
    task = sqs.processors.connect(
        queue, 'say_hello', raise_exception, backoff_policy=IMMEDIATE_RETURN)
    task.delay(username='Homer')
    assert sqs.process_batch(queue, wait_seconds=0).failed_count() == 1

    # re-connect a non-broken processor for the queue
    sqs.processors.connect(queue, 'say_hello', say_hello)
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 1


def test_redrive(sqs, queue_with_redrive):
    if isinstance(sqs, MemoryEnv):
        pytest.skip('Redrive not implemented with MemoryEnv')

    queue, dead_queue = queue_with_redrive

    # add processor which fails to the standard queue
    task = sqs.processors.connect(
        queue, 'say_hello', raise_exception, backoff_policy=IMMEDIATE_RETURN)

    # add message to the queue and process it twice
    # the message has to be moved to dead letter queue
    task.delay(username='Homer')
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 0
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 0

    # add processor which succeeds
    sqs.processors.connect(dead_queue, 'say_hello', say_hello)
    assert sqs.process_batch(dead_queue, wait_seconds=0).succeeded_count() == 1


def test_dead_letter_processor(sqs, queue_with_redrive):
    sqs.processors.fallback_processor_maker = DeadLetterProcessor
    queue, dead_queue = queue_with_redrive

    # dead queue doesn't have a processor, so a dead letter processor
    # will be fired, and it will mark the task as successful
    sqs.add_job(dead_queue, 'say_hello')
    assert sqs.process_batch(dead_queue, wait_seconds=0).succeeded_count() == 1

    # queue has processor which succeeds (but we need to wait at least
    # 1 second for this task to appear here)
    sqs.processors.connect(queue, 'say_hello', say_hello)
    assert sqs.process_batch(queue, wait_seconds=2).succeeded_count() == 1


def test_exponential_backoff_works(sqs, queue):
    task = sqs.processors.connect(
        queue,
        'say_hello',
        raise_exception,
        backoff_policy=ExponentialBackoff(0.1, max_visbility_timeout=0.1))
    task.delay(username='Homer')
    assert sqs.process_batch(queue, wait_seconds=0).failed_count() == 1


def test_drain_queue(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay(username='One')
    say_hello_task.delay(username='Two')
    sqs.drain_queue(queue, wait_seconds=0)
    assert sqs.process_batch(queue, wait_seconds=0).succeeded_count() == 0
    assert worker_results['say_hello'] is None


def test_message_retention_period(sqs, random_queue_name):
    try:
        sqs.create_standard_queue(
            random_queue_name, message_retention_period=600)
        sqs.create_fifo_queue(
            random_queue_name + '.fifo', message_retention_period=600)
    finally:
        try:
            sqs.delete_queue(random_queue_name)
        except Exception:
            pass
        try:
            sqs.delete_queue(random_queue_name + '.fifo')
        except Exception:
            pass


def test_deduplication_id(sqs, fifo_queue):
    if isinstance(sqs, MemoryEnv):
        pytest.skip('Deduplication id not implemented with MemoryEnv')

    say_hello_task = sqs.processors.connect(fifo_queue, 'say_hello', say_hello)
    say_hello_task.delay(username='One', _deduplication_id='x')
    say_hello_task.delay(username='Two', _deduplication_id='x')
    assert sqs.process_batch(fifo_queue, wait_seconds=0).succeeded_count() == 1
    assert worker_results['say_hello'] == 'One'


def test_group_id(sqs, fifo_queue):
    # not much we can test here, but at least test that it doesn't blow up
    # and that group_id and deduplication_id are orthogonal
    if isinstance(sqs, MemoryEnv):
        pytest.skip('Deduplication id not implemented with MemoryEnv')

    say_hello_task = sqs.processors.connect(fifo_queue, 'say_hello', say_hello)
    say_hello_task.delay(username='One', _deduplication_id='x', _group_id='g1')
    say_hello_task.delay(username='Two', _deduplication_id='x', _group_id='g2')
    assert sqs.process_batch(fifo_queue, wait_seconds=0).succeeded_count() == 1
    assert worker_results['say_hello'] == 'One'


def test_delay_seconds(sqs, queue):
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay(username='Homer', _delay_seconds=2)
    assert sqs.process_batch(queue, wait_seconds=1).succeeded_count() == 0
    time.sleep(3)
    assert sqs.process_batch(queue, wait_seconds=1).succeeded_count() == 1


def test_visibility_timeout(sqs, random_queue_name):
    try:
        sqs.create_standard_queue(random_queue_name, visibility_timeout=1)
        sqs.create_fifo_queue(
            random_queue_name + '.fifo', visibility_timeout=1)
    finally:
        try:
            sqs.delete_queue(random_queue_name)
        except Exception:
            pass
        try:
            sqs.delete_queue(random_queue_name + '.fifo')
        except Exception:
            pass


def test_custom_processor(sqs, queue):
    class CustomProcessor(Processor):
        def process(self, job_kwargs, job_context):
            job_kwargs['username'] = 'Foo'
            super(CustomProcessor, self).process(job_kwargs, job_context)

    sqs.processors.processor_maker = CustomProcessor
    say_hello_task = sqs.processors.connect(queue, 'say_hello', say_hello)
    say_hello_task.delay()
    assert sqs.process_batch(queue).succeeded_count() == 1
    assert worker_results['say_hello'] == 'Foo'


def test_custom_batch_processor(sqs, queue):
    class CustomBatchProcessor(BatchProcessor):
        def process(self, jobs, context):
            jobs[0]['username'] = 'Two'
            super(CustomBatchProcessor, self).process(jobs, context)

    sqs.processors.batch_processor_maker = CustomBatchProcessor
    task = sqs.processors.connect_batch(queue, 'batch_say_hello',
                                       batch_say_hello)

    task.delay(username='One')
    assert sqs.process_batch(queue).succeeded_count() == 1
    assert worker_results['batch_say_hello'] == {'Two'}
