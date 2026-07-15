def failed_steps(lines):
    return [line.split()[1] for line in lines if line.startswith('STEP') and line.split()[-1] != 'OK']
