class BookingService
  Result = Struct.new(:success?, :booking, :error, keyword_init: true)

  def self.create(client:, freelancer:, start_date:, end_date:, total_amount:)
    unless freelancer.availability?
      return Result.new(success?: false, error: "Freelancer is not available")
    end

    if conflicting_booking?(freelancer, start_date, end_date)
      return Result.new(success?: false, error: "Freelancer has a conflicting booking")
    end

    booking = Booking.new(
      client: client,
      freelancer: freelancer,
      start_date: start_date,
      end_date: end_date,
      total_amount: total_amount
    )

    if booking.save
      Result.new(success?: true, booking: booking)
    else
      Result.new(success?: false, error: booking.errors.full_messages.join(", "))
    end
  end

  def self.conflicting_booking?(freelancer, start_date, end_date)
    freelancer.bookings
              .where.not(status: :cancelled)
              .where("start_date < ? AND end_date > ?", end_date, start_date)
              .exists?
  end

  private_class_method :conflicting_booking?
end
