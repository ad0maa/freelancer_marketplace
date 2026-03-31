module Api
  module V1
    class BookingsController < ApplicationController
      def index
        bookings = Booking.all
        render json: bookings
      end

      def create
        booking = Booking.new(booking_params)

        if booking.save
          render json: booking, status: :created
        else
          render json: { errors: booking.errors }, status: :unprocessable_content
        end
      end

      def confirm
        transition_status(:pending, :confirmed)
      end

      def complete
        transition_status(:confirmed, :completed)
      end

      def cancel
        transition_status(%i[pending confirmed], :cancelled)
      end

      private

      def booking_params
        params.expect(booking: %i[client_id freelancer_id start_date end_date total_amount])
      end

      def transition_status(from, to)
        booking = Booking.find(params[:id])
        allowed = Array(from)

        if allowed.include?(booking.status.to_sym)
          booking.update!(status: to)
          render json: booking
        else
          render json: { error: "Cannot transition from #{booking.status} to #{to}" },
                 status: :unprocessable_content
        end
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Booking not found" }, status: :not_found
      end
    end
  end
end
