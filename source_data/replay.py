"""按声明顺序返回不解释的源响应，用于离线检查资料接口。"""
import copy

class Replay:
    def __init__(self,steps):self.steps=copy.deepcopy(steps);self.position=0;self.calls=[]
    def call(self,operation,arguments):
        if self.position>=len(self.steps):raise RuntimeError('源响应已经耗尽')
        step=self.steps[self.position]
        if step['operation']!=operation:raise ValueError('源操作顺序不符')
        if 'arguments' in step and step['arguments']!=arguments:raise ValueError('源请求参数不符')
        self.position+=1;self.calls.append({'operation':operation,'arguments':copy.deepcopy(arguments)})
        if 'error' in step:raise RuntimeError(step['error'])
        return copy.deepcopy(step['response'])
    def remaining(self):return len(self.steps)-self.position
