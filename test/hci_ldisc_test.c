/* Quick test: separate TIOCSETD (N_HCI) from HCIUARTSETPROTO (H4) */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#define N_HCI 15
#define HCIUARTSETPROTO _IOW('U', 200, int)
#define HCI_UART_H4 0

int main(int argc, char *argv[]) {
    const char *dev = argc > 1 ? argv[1] : "/dev/ttyAMA10";
    int fd = open(dev, O_RDWR | O_NOCTTY);
    if (fd < 0) { fprintf(stderr, "open %s: %s\n", dev, strerror(errno)); return 1; }

    struct termios ti;
    tcgetattr(fd, &ti);
    cfmakeraw(&ti);
    ti.c_cflag |= CLOCAL;
    ti.c_cflag &= ~CRTSCTS;
    cfsetispeed(&ti, B115200);
    cfsetospeed(&ti, B115200);
    tcsetattr(fd, TCSANOW, &ti);
    tcflush(fd, TCIOFLUSH);

    /* Step 1: set N_HCI line discipline */
    int ldisc = N_HCI;
    if (ioctl(fd, TIOCSETD, &ldisc) < 0) {
        fprintf(stderr, "TIOCSETD N_HCI: errno=%d %s\n", errno, strerror(errno));
        close(fd);
        return 1;
    }
    fprintf(stderr, "TIOCSETD N_HCI: OK\n");

    /* Step 2: set HCI UART H4 protocol */
    int proto = HCI_UART_H4;
    if (ioctl(fd, HCIUARTSETPROTO, &proto) < 0) {
        fprintf(stderr, "HCIUARTSETPROTO H4: errno=%d %s\n", errno, strerror(errno));
        /* Try to read back what protos are available */
        int cur = -1;
        if (ioctl(fd, _IOR('U', 201, int), &cur) == 0)
            fprintf(stderr, "  current proto: %d\n", cur);
        /* Restore ldisc */
        ldisc = 0;
        ioctl(fd, TIOCSETD, &ldisc);
        close(fd);
        return 1;
    }
    fprintf(stderr, "HCIUARTSETPROTO H4: OK\n");

    /* Check for hci device */
    int devid = -1;
    if (ioctl(fd, _IOR('U', 202, int), &devid) == 0)
        fprintf(stderr, "Created hci%d\n", devid);

    /* Restore */
    ldisc = 0;
    ioctl(fd, TIOCSETD, &ldisc);
    close(fd);
    return 0;
}
