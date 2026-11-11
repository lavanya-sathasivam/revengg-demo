import subprocess
import sys
args = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
result = subprocess.run(args, capture_output=True, text=True, cwd=r'D:\Desktop\revengg-demo')
with open('unittest_output.txt', 'w') as f:
    f.write('STDOUT:\n' + result.stdout)
    f.write('STDERR:\n' + result.stderr)
    f.write('RC:' + str(result.returncode))
print('Done')