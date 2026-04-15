/* Scan all HCI UART protocols to see which ones are available */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#define N_HCI 15
#define HCIUARTSETPROTO _IOW('U', 200, int)
#define HCIUARTGETPROTO _IOR('U', 201, int)
#define HCIUARTGETDEVID _IOR('U', 202, int)
#define HCIUARTSETFLAGS _IOW('U', 203, int)

static const char *proto_names[] = {
    "H4", "BCSP", "3WIRE", "H4DS", "LL", "ATH3K", "INTEL",
    "BCM", "QCA", "AG6XX", "MRVL", "AML"
};

int main(int argc, char *argv[]) {
    const char *dev = argc > 1 ? argv[1] : "/dev/ttyAMA10";

    for (int proto = 0; proto < 12; proto++) {
        int fd = open(dev, O_RDWR | O_NOCTTY);
        if (fd < 0) { fprintf(stderr, "open: %s\n", strerror(errno)); return 1; }

        struct termios ti;
        tcgetattr(fd, &ti);
        cfmakeraw(&ti);
        ti.c_cflag |= CLOCAL;
        ti.c_cflag &= ~CRTSCTS;
        cfsetispeed(&ti, B115200);
        cfsetospeed(&ti, B115200);
        tcsetattr(fd, TCSANOW, &ti);
        tcflush(fd, TCIOFLUSH);

        int ldisc = N_HCI;
        if (ioctl(fd, TIOCSETD, &ldisc) < 0) {
            fprintf(stderr, "proto %d (%s): TIOCSETD failed: %s\n",
                    proto, proto_names[proto], strerror(errno));
            close(fd);
            continue;
        }

        int ret = ioctl(fd, HCIUARTSETPROTO, proto);
        if (ret < 0) {
            fprintf(stderr, "proto %d (%s): HCIUARTSETPROTO: errno=%d %s\n",
                    proto, proto_names[proto], errno, strerror(errno));
        } else {
            int devid = -1;
            ioctl(fd, HCIUARTGETDEVID, &devid);
            fprintf(stderr, "proto %d (%s): OK! hci%d\n",
                    proto, proto_names[proto], devid);
        }

        ldisc = 0;
        ioctl(fd, TIOCSETD, &ldisc);
        close(fd);

        if (ret == 0) break; /* Found working proto, stop */
    }
    return 0;
}
