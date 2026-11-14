"""Keep the dedicated local publisher alive across ten-minute windows."""
import os
import time
from .catalogue_publish import main


def run(duration=105*60, clock=time.time, sleep=time.sleep, publish=main):
    if os.environ.get('FMR_D1_WRITES_PAUSED')=='true':
        print('D1 发布已暂停；不进入驻留发布循环。',flush=True)
        return 0
    if os.environ.get('FMR_CATALOGUE_PUBLISH_ONCE')=='true':
        return publish() or 0
    deadline=clock()+duration
    last_slot=None
    failed_windows=0
    while clock()<deadline:
        slot=int(clock())//600
        if slot!=last_slot:
            print('发布执行器存活：开始本 10 分钟窗口的增量同步。',flush=True)
            try:
                if publish():failed_windows+=1
            except Exception as exc:
                failed_windows+=1
                # main() saves successful acknowledgements in its finally block.
                # A failed network request must not turn into a tight retry loop.
                print('发布周期失败：'+type(exc).__name__+'；确认点已保留，下个窗口重试。',flush=True)
            last_slot=slot
        target=min(deadline,(int(clock())//600+1)*600)
        while clock()<target:
            sleep(min(30,max(0,target-clock())))
            print('发布执行器心跳：等待下个 10 分钟窗口。',flush=True)
    print('本执行器驻留期结束；上传确认点后由工作流交接下一执行器。',flush=True)
    if failed_windows:
        print(f'本次驻留有 {failed_windows} 个发布窗口失败。',flush=True)
    return int(failed_windows>0)


if __name__=='__main__':
    raise SystemExit(run())
