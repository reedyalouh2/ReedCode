import os
import shutil
import stat
import sys
from contextlib import ExitStack

root, relative, operation = sys.argv[1:4]
if relative.startswith("/workspace/"):
    relative = relative[len("/workspace/") :]
parts = relative.split("/")
if any(part in ("", ".", "..") for part in parts):
    raise SystemExit("ERROR: expected a file inside the workspace")
try:
    with ExitStack() as stack:

        def hold(fd):
            stack.callback(os.close, fd)
            return fd

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = hold(os.open(root, directory_flags))
        for part in parts[:-1]:
            if operation == "write":
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = hold(os.open(part, directory_flags, dir_fd=parent))
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_WRONLY | os.O_CREAT if operation == "write" else os.O_RDONLY
        fd = hold(os.open(parts[-1], flags, 0o666, dir_fd=parent))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("file tools require a regular file")
        if operation == "write":
            if metadata.st_nlink != 1:
                raise ValueError("write_file refuses files with multiple hard links")
            content = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError("write_file content exceeds 2 MiB")
            os.ftruncate(fd, 0)
            with os.fdopen(os.dup(fd), "wb") as file:
                file.write(content)
        else:
            with os.fdopen(os.dup(fd), "rb") as file:
                shutil.copyfileobj(file, sys.stdout.buffer)
except (OSError, ValueError) as error:
    raise SystemExit("ERROR: " + str(error))
