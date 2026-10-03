#ifndef MOSH_SERVER_STATUS_H
#define MOSH_SERVER_STATUS_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

class ServerStatus {
  int listener;
  std::string path;
  dev_t device;
  ino_t inode;

  static bool private_directory( const std::string &name )
  {
    struct stat info;
    return lstat( name.c_str(), &info ) == 0
      && S_ISDIR( info.st_mode ) && info.st_uid == getuid()
      && (info.st_mode & 0077) == 0;
  }

public:
  ServerStatus() : listener( -1 ), device( 0 ), inode( 0 )
  {
#ifdef __linux__
    char user[32];
    snprintf( user, sizeof user, "%lu", static_cast<unsigned long>( getuid() ) );
    const char *runtime = getenv( "XDG_RUNTIME_DIR" );
    std::string base = runtime && runtime[0] == '/' ? runtime : std::string( "/run/user/" ) + user;
    std::string directory = private_directory( base )
      ? base + "/mosh-status" : std::string( "/tmp/mosh-status-" ) + user;
    mkdir( directory.c_str(), 0700 );
    if ( !private_directory( directory ) ) return;

    char pid[32];
    snprintf( pid, sizeof pid, "%ld", static_cast<long>( getpid() ) );
    std::string candidate = directory + "/" + pid + ".sock";
    struct sockaddr_un address;
    memset( &address, 0, sizeof address );
    address.sun_family = AF_UNIX;
    if ( candidate.size() >= sizeof address.sun_path ) return;
    memcpy( address.sun_path, candidate.c_str(), candidate.size() + 1 );

    listener = socket( AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0 );
    if ( listener < 0 ) return;
    if ( bind( listener, reinterpret_cast<struct sockaddr *>( &address ), sizeof address ) < 0 ) {
      close( listener );
      listener = -1;
      return;
    }
    path = candidate;
    struct stat info;
    if ( lstat( path.c_str(), &info ) == 0 ) {
      device = info.st_dev;
      inode = info.st_ino;
    }
    if ( chmod( path.c_str(), 0600 ) < 0 || listen( listener, 8 ) < 0 ) {
      close( listener );
      listener = -1;
    }
#endif
  }

  ~ServerStatus()
  {
    if ( listener >= 0 ) close( listener );
    struct stat info;
    if ( !path.empty() && lstat( path.c_str(), &info ) == 0
         && info.st_dev == device && info.st_ino == inode ) {
      unlink( path.c_str() );
    }
  }

  int fd() const { return listener; }

  void respond( uint64_t last_heard ) const
  {
#ifdef __linux__
    // One small reply per query. Packet receipt already maintains last_heard.
    for ( int count = 0; count < 8; count++ ) {
      int client = accept4( listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC );
      if ( client < 0 ) return;
      struct ucred credentials;
      socklen_t length = sizeof credentials;
      if ( getsockopt( client, SOL_SOCKET, SO_PEERCRED, &credentials, &length ) == 0
           && credentials.uid == getuid() ) {
        char timestamp[32];
        if ( last_heard == uint64_t( -1 ) ) {
          strcpy( timestamp, "null" );
        } else {
          snprintf( timestamp, sizeof timestamp, "%llu", static_cast<unsigned long long>( last_heard ) );
        }
        char response[160];
        int size = snprintf( response, sizeof response,
          "{\"version\":1,\"pid\":%ld,\"last_rx_monotonic_ms\":%s}\n",
          static_cast<long>( getpid() ), timestamp );
        send( client, response, size, MSG_DONTWAIT | MSG_NOSIGNAL );
      }
      close( client );
    }
#else
    (void)last_heard;
#endif
  }

private:
  ServerStatus( const ServerStatus & );
  ServerStatus &operator=( const ServerStatus & );
};

#endif
